"""Review backend: every rule is checked by calling the database functions the way the screen will,
as different signed-in people (reviewer, admin, inactive, someone with no role) and as anon.

`as_user` does what Supabase does for a request: it switches to the `authenticated` role and sets the
caller's id, which is what auth.uid() reads."""
import json
import threading
import uuid
from contextlib import contextmanager

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege, RaiseException
from psycopg import sql

from pipeline import db

CROP = {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}


@contextmanager
def as_user(conn, uid):
    conn.execute("select set_config('request.jwt.claim.sub', %s, false)", (str(uid or ""),))
    conn.execute("set role anon" if uid is None else "set role authenticated")
    try:
        yield
    finally:
        conn.execute("reset role")
        conn.execute("select set_config('request.jwt.claim.sub', '', false)")


def api(conn, who, name, *args):
    """Call one database function as `who` (a user id, or None for anon) and return its result.

    The three steps (become the caller, call, step back) go out as one multi-statement query, so it
    costs one network round trip. If the call fails, Postgres rolls the whole query back, which also
    undoes the role switch."""
    def literal(a):
        return sql.SQL("{}::jsonb").format(sql.Literal(json.dumps(a))) if isinstance(a, (dict, list)) else sql.Literal(a)
    query = sql.SQL("select set_config('request.jwt.claim.sub', {who}, false), set_config('role', {role}, false); "
                    "select {fn}({args}); reset role").format(
        who=sql.Literal(str(who or "")), role=sql.Literal("anon" if who is None else "authenticated"),
        fn=sql.SQL(name), args=sql.SQL(", ").join(literal(a) for a in args))
    cur = conn.execute(query)
    cur.nextset()
    return cur.fetchone()[0]


def fails(match, kind=RaiseException):
    return pytest.raises(kind, match=match)


@pytest.fixture(autouse=True)
def open_schema_to_roles(test_schema):
    """The test schema is not `public`, so let the API roles look inside it (they reach tables only
    through the functions, which is what these tests are about)."""
    admin = psycopg.connect(__import__("os").environ["DATABASE_URL"], autocommit=True)
    admin.execute(f'grant usage on schema "{test_schema}" to anon, authenticated')
    admin.close()


@pytest.fixture
def users(conn):
    ids = {}
    for name, role, active in [("admin", "admin", True), ("r1", "reviewer", True), ("r2", "reviewer", True),
                               ("r3", "reviewer", True), ("gone", "reviewer", False)]:
        ids[name] = uuid.uuid4()
        conn.execute("insert into auth.users (id, email) values (%s, %s)", (ids[name], f"{name}-{ids[name].hex[:8]}@t.example"))
        conn.execute("insert into app_user (user_id, email, role, active) values (%s, %s, %s, %s)",
                     (ids[name], f"{name}@t.example", role, active))
    ids["stranger"] = uuid.uuid4()       # signed in, but never given a role
    conn.execute("insert into auth.users (id, email) values (%s, %s)", (ids["stranger"], f"stranger-{ids['stranger'].hex[:8]}@t.example"))
    yield ids
    for table in ("review_edit", "review_page_seen", "review"):
        conn.execute(f"delete from {table}")
    conn.execute("delete from auth.users where id = any(%s)", (list(ids.values()),))


def add_catalog(conn, name, pages, statuses):
    brand = conn.execute("insert into brand (brand_name) values (%s) on conflict do nothing returning brand_id", (name,)).fetchone()
    brand_id = brand[0] if brand else conn.execute("select brand_id from brand where brand_name = %s", (name,)).fetchone()[0]
    cid = conn.execute("insert into catalog (catalog_name, brand_id, file_sha256, storage_path, page_count) "
                       "values (%s, %s, %s, 'pdfs/x.pdf', %s) returning catalog_id",
                       (name, brand_id, uuid.uuid4().hex * 2, pages)).fetchone()[0]
    ex = {}
    for page, (status, ptype) in statuses.items():
        ex[page] = conn.execute("insert into page_extractions (catalog_id, page, model, status, page_type, dpi, model_dpi) "
                                "values (%s, %s, 'm', %s, %s, 72, 72) returning extraction_id",
                                (cid, page, status, ptype)).fetchone()[0]
    return cid, ex


@pytest.fixture
def cat(conn):
    """A 4-page catalogue: page 1 has two products, page 2 one, page 3 is non-product, page 4 failed."""
    cid, ex = add_catalog(conn, "Acme", 4, {1: ("ok", "product"), 2: ("ok", "product"),
                                            3: ("ok", "non_product"), 4: ("error", None)})
    def product(page, pos, name, price, path):
        return conn.execute("insert into product (extraction_id, position, product_name, price_amount, cropped_path) "
                            "values (%s, %s, %s, %s, %s) returning product_id", (ex[page], pos, name, price, path)).fetchone()[0]
    return {"cid": cid, "ex": ex, "cup": product(1, 1, "Cup", 100, "cup.webp"),
            "mug": product(1, 2, "Mug", 50, "mug.webp"), "jar": product(2, 1, "Jar", 20, None)}


def view_all(conn, who, cid, lvl, pages=4):
    for p in range(1, pages + 1):
        api(conn, who, "rv_get_page", cid, lvl, p)


def level_1_done(conn, users, cid, pages=4):
    api(conn, users["r1"], "rv_open", cid)
    view_all(conn, users["r1"], cid, 1, pages)
    return api(conn, users["r1"], "rv_finish", cid, 1)


# ---- who may call what -------------------------------------------------------------------------

def test_anon_cannot_call_any_function(conn, users, cat):
    for name, args in [("rv_me", ()), ("rv_list_catalogs", ()), ("rv_open", (cat["cid"],)), ("rv_lists", ()),
                       ("admin_list_users", ()), ("rv_get_page", (cat["cid"], 1, 1))]:
        with fails("permission denied", InsufficientPrivilege):
            api(conn, None, name, *args)


def test_internal_helpers_are_not_callable_by_signed_in_users(conn, users, cat):
    with fails("permission denied", InsufficientPrivilege):
        api(conn, users["r1"], "rvx_actor")
    with fails("permission denied", InsufficientPrivilege):
        api(conn, users["admin"], "rvx_log", None, None, "x", None, None, None)


@pytest.mark.parametrize("who", ["stranger", "gone"])
def test_people_without_an_active_role_can_do_nothing(conn, users, cat, who):
    for name, args in [("rv_me", ()), ("rv_list_catalogs", ()), ("rv_lists", ()), ("rv_open", (cat["cid"],)),
                       ("rv_add_category", ("X",)), ("admin_list_users", ())]:
        with fails("not an active user", InsufficientPrivilege):
            api(conn, users[who], name, *args)


def test_the_tables_cannot_be_read_or_written_directly(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    for table in ("app_user", "review", "review_edit", "review_page_seen", "review_log", "product", "catalog"):
        with as_user(conn, users["r1"]):
            try:
                rows = conn.execute(f"select count(*) from {table}").fetchone()[0]
            except InsufficientPrivilege:
                rows = 0
        assert rows == 0, table
    with as_user(conn, users["admin"]):
        for write in ("update app_user set role = 'admin'", "delete from review", "update product set product_name = 'x'"):
            try:
                changed = conn.execute(write).rowcount
            except InsufficientPrivilege:
                changed = 0
            assert changed == 0, write


def test_new_tables_have_row_level_security_on(conn):
    off = conn.execute("select c.relname from pg_class c where c.relnamespace = current_schema()::regnamespace "
                       "and c.relname in ('app_user','review_edit','review_page_seen','review_log') and not c.relrowsecurity").fetchall()
    assert off == []


def test_me_says_who_you_are(conn, users):
    assert api(conn, users["admin"], "rv_me")["role"] == "admin"
    assert api(conn, users["r1"], "rv_me")["role"] == "reviewer"


# ---- opening a catalogue ------------------------------------------------------------------------

def test_the_list_shows_state_and_what_you_can_open(conn, users, cat):
    add_catalog(conn, "Late", 3, {1: ("ok", "product"), 2: ("submitted", None)})        # page 3 not even submitted
    by_name = {c["catalog_name"]: c for c in api(conn, users["r1"], "rv_list_catalogs")}
    assert by_name["Late"]["state"] == "not_ready" and by_name["Late"]["can_open"] is False
    assert by_name["Acme"]["state"] == "level_1_open" and by_name["Acme"]["can_open"] is True
    api(conn, users["r1"], "rv_open", cat["cid"])
    mine = {c["catalog_name"]: c for c in api(conn, users["r1"], "rv_list_catalogs")}["Acme"]
    theirs = {c["catalog_name"]: c for c in api(conn, users["r2"], "rv_list_catalogs")}["Acme"]
    assert mine["state"] == theirs["state"] == "level_1_in_progress"
    assert mine["mine"] is True and mine["can_open"] is True
    assert theirs["can_open"] is False and "another reviewer" in theirs["blocked_reason"]
    assert theirs["level_1_by"] == "r1@t.example"


def test_open_refuses_unready_and_unknown_catalogues(conn, users, cat):
    late, _ = add_catalog(conn, "Late", 2, {1: ("ok", "product"), 2: ("reserved", None)})
    with fails("not ready for review"):
        api(conn, users["r1"], "rv_open", late)
    with fails("unknown catalogue"):
        api(conn, users["r1"], "rv_open", 987654321)
    nopages = conn.execute("insert into catalog (catalog_name, brand_id, file_sha256, storage_path) "
                           "values ('NoCount', (select brand_id from brand limit 1), %s, 'p') returning catalog_id",
                           (uuid.uuid4().hex * 2,)).fetchone()[0]
    with fails("not ready for review"):
        api(conn, users["r1"], "rv_open", nopages)


def test_open_claims_level_1_once_and_resumes_for_the_same_person(conn, users, cat):
    first = api(conn, users["r1"], "rv_open", cat["cid"])
    assert first["level"] == 1 and first["page_count"] == 4 and first["storage_path"] == "pdfs/x.pdf"
    assert api(conn, users["r1"], "rv_open", cat["cid"])["level"] == 1          # resume, not a second claim
    with fails("Level 1 is being done by another reviewer"):
        api(conn, users["r2"], "rv_open", cat["cid"])
    assert conn.execute("select count(*) from review_log where action = 'claim'").fetchone()[0] == 1


def test_two_people_opening_at_once_only_one_wins(conn, users, cat):
    barrier = threading.Barrier(2)
    outcome = {}

    def worker(name):
        with db.connect(autocommit=True) as c:
            barrier.wait()
            try:
                api(c, users[name], "rv_open", cat["cid"])
                outcome[name] = "won"
            except RaiseException:
                outcome[name] = "blocked"

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("r1", "r2")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(outcome.values()) == ["blocked", "won"]


# ---- looking at pages ---------------------------------------------------------------------------

def test_only_the_person_who_opened_a_level_can_use_it(conn, users, cat):
    with fails("have not opened level 1", InsufficientPrivilege):
        api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)          # nobody opened it yet
    api(conn, users["r1"], "rv_open", cat["cid"])
    for who in ("r2", "admin"):
        with fails("have not opened level 1", InsufficientPrivilege):
            api(conn, users[who], "rv_get_page", cat["cid"], 1, 1)
        with fails("have not opened level 1", InsufficientPrivilege):
            api(conn, users[who], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"product_name": "Hack"})
        with fails("have not opened level 1", InsufficientPrivilege):
            api(conn, users[who], "rv_finish", cat["cid"], 1)
    with fails("have not opened level 2", InsufficientPrivilege):
        api(conn, users["r1"], "rv_get_page", cat["cid"], 2, 1)
    with fails("level must be 1 or 2"):
        api(conn, users["r1"], "rv_get_page", cat["cid"], 3, 1)


def test_get_page_shows_products_and_marks_the_page_seen(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    page = api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)
    assert page["page_type"] == "product" and page["failed"] is False
    assert [p["product_name"] for p in page["products"]] == ["Cup", "Mug"]
    assert page["products"][0]["price_amount"] == 100 and page["products"][0]["crop"] is None
    assert api(conn, users["r1"], "rv_progress", cat["cid"], 1)["seen_pages"] == [1]


def test_non_product_and_failed_pages_show_no_products(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    non_product = api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 3)
    failed = api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 4)
    assert (non_product["page_type"], non_product["failed"], non_product["products"]) == ("non_product", False, [])
    assert (failed["page_type"], failed["failed"], failed["products"]) == ("non_product", True, [])


def test_page_numbers_are_checked(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    for bad in (0, 5, -1, None):
        with fails("page must be between 1 and 4"):
            api(conn, users["r1"], "rv_get_page", cat["cid"], 1, bad)
        with fails("page must be between 1 and 4"):
            api(conn, users["r1"], "rv_add_product", cat["cid"], 1, bad)


def test_progress_tracks_pages_seen_and_products(conn, users, cat):
    first = api(conn, users["r1"], "rv_open", cat["cid"])
    assert first["pages_seen"] == 0 and first["can_finish"] is False and first["resume_page"] == 1
    assert first["products_per_page"] == [2, 1, 0, 0] and first["product_count"] == 3
    api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)
    api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 2)
    api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, cat["mug"], None)
    api(conn, users["r1"], "rv_add_product", cat["cid"], 1, 3, {"product_name": "New", "price_amount": 5})
    now = api(conn, users["r1"], "rv_progress", cat["cid"], 1)
    assert now["resume_page"] == 3 and now["products_per_page"] == [1, 1, 1, 0] and now["edit_count"] == 2
    view_all(conn, users["r1"], cat["cid"], 1)
    assert api(conn, users["r1"], "rv_progress", cat["cid"], 1)["can_finish"] is True


# ---- editing ------------------------------------------------------------------------------------

def test_saving_a_product_is_a_draft_until_finish(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    saved = api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None,
                {"product_name": "  Big Cup ", "price_amount": 120.456, "tags": ["Red", "red", " blue "], "crop": CROP,
                 "weight": 0.25, "uom": "", "variant": "L"})
    assert saved["product_name"] == "Big Cup" and saved["price_amount"] == 120.46 and saved["tags"] == ["blue", "red"]
    assert saved["uom"] is None and saved["crop"] == {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4} and saved["edited"] is True
    again = api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"price_amount": 130})
    assert again["product_name"] == "Big Cup" and again["price_amount"] == 130          # edits merge
    assert conn.execute("select product_name, price_amount from product where product_id = %s", (cat["cup"],)).fetchone() == ("Cup", 100)
    shown = api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)["products"][0]
    assert shown["product_name"] == "Big Cup" and shown["edited"] is True


@pytest.mark.parametrize("fields,message", [
    ({"nope": 1}, "unknown field nope"),
    ({"product_name": ""}, "product_name must be non-empty"),
    ({"product_name": "   "}, "product_name must be non-empty"),
    ({"product_name": None}, "product_name must be non-empty"),
    ({"product_name": "x" * 301}, "too long"),
    ({"price_amount": -1}, "out of range"),
    ({"price_amount": "12"}, "must be a number or null"),
    ({"price_amount": 1e12}, "out of range"),
    ({"weight": -0.1}, "out of range"),
    ({"price_currency": ""}, "price_currency must be non-empty"),
    ({"sub_id": 123456789}, "not an existing subcategory"),
    ({"sub_id": 1.5}, "not an existing subcategory"),
    ({"tags": "red"}, "list of text"),
    ({"tags": [1]}, "list of text"),
    ({"tags": ["x" * 51]}, "tag is too long"),
    ({"tags": [f"t{i}" for i in range(31)]}, "too many tags"),
    ({"crop": {"x": 0, "y": 0, "w": 1.2, "h": 1}}, "inside the page"),
    ({"crop": {"x": 0.9, "y": 0, "w": 0.5, "h": 1}}, "inside the page"),
    ({"crop": {"x": 0, "y": 0, "w": 0.001, "h": 1}}, "inside the page"),
    ({"crop": {"x": -0.1, "y": 0, "w": 0.5, "h": 0.5}}, "inside the page"),
    ({"crop": {"x": 0, "y": 0, "w": 0.5}}, "crop must be"),
    ({"crop": {"x": 0, "y": 0, "w": 0.5, "h": 0.5, "z": 1}}, "crop must be"),
    ({"crop": {"x": "0", "y": 0, "w": 0.5, "h": 0.5}}, "crop must be"),
    ({"crop": "whole"}, "crop must be"),
    ({"crop": {"none": False}}, "crop must be"),
])
def test_bad_fields_are_refused_and_nothing_is_saved(conn, users, cat, fields, message):
    api(conn, users["r1"], "rv_open", cat["cid"])
    with fails(message):
        api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, fields)
    assert conn.execute("select count(*) from review_edit").fetchone()[0] == 0


def test_fields_must_be_an_object_and_one_target_must_be_named(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    with fails("fields must be a JSON object"):
        api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, ["a"])
    with fails("exactly one of product_id and edit_id"):
        api(conn, users["r1"], "rv_save_product", cat["cid"], 1, None, None, {"product_name": "x"})
    with fails("exactly one of product_id and edit_id"):
        api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], 1, {"product_name": "x"})
    with fails("exactly one of product_id and edit_id"):
        api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, None, None)


def test_crop_can_be_none_or_reset_to_the_whole_page(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    none = api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"crop": {"none": True}})
    assert none["crop"] == {"none": True}
    whole = api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"crop": None})
    assert whole["crop"] is None


def test_a_product_from_another_catalogue_cannot_be_touched(conn, users, cat):
    other, ex = add_catalog(conn, "Other", 1, {1: ("ok", "product")})
    foreign = conn.execute("insert into product (extraction_id, position, product_name) values (%s, 1, 'Foreign') returning product_id",
                           (ex[1],)).fetchone()[0]
    api(conn, users["r1"], "rv_open", cat["cid"])
    for call in (lambda: api(conn, users["r1"], "rv_save_product", cat["cid"], 1, foreign, None, {"product_name": "x"}),
                 lambda: api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, foreign, None)):
        with fails("not in this catalogue"):
            call()


def test_added_products_can_be_edited_and_discarded(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    added = api(conn, users["r1"], "rv_add_product", cat["cid"], 1, 3, {"product_name": "Lamp"})
    assert added["product_id"] is None and added["edit_id"] and added["page"] == 3 and added["price_currency"] == "INR"
    saved = api(conn, users["r1"], "rv_save_product", cat["cid"], 1, None, added["edit_id"], {"price_amount": 9})
    assert saved["product_name"] == "Lamp" and saved["price_amount"] == 9
    assert [p["product_name"] for p in api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 3)["products"]] == ["Lamp"]
    api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, None, added["edit_id"])
    assert api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 3)["products"] == []
    with fails("does not exist in this draft"):
        api(conn, users["r1"], "rv_save_product", cat["cid"], 1, None, added["edit_id"], {"price_amount": 1})
    with fails("does not exist in this draft"):
        api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, None, added["edit_id"])


def test_removed_products_show_as_removed_and_can_be_restored(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["mug"], None, {"price_amount": 5})
    api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, cat["mug"], None)
    api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, cat["mug"], None)        # twice is harmless
    mug = api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)["products"][1]
    assert mug["removed"] is True and mug["price_amount"] == 50                          # its earlier edit was dropped
    with fails("marked removed"):
        api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["mug"], None, {"price_amount": 6})
    api(conn, users["r1"], "rv_restore_product", cat["cid"], 1, cat["mug"])
    assert api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)["products"][1]["removed"] is False
    with fails("not marked removed"):
        api(conn, users["r1"], "rv_restore_product", cat["cid"], 1, cat["mug"])


def test_brand_change_is_a_draft_and_naming_the_same_brand_clears_it(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["r1"], "rv_set_brand", cat["cid"], 1, "Zeta")
    assert api(conn, users["r1"], "rv_progress", cat["cid"], 1)["brand_name"] == "Zeta"
    api(conn, users["r1"], "rv_set_brand", cat["cid"], 1, " ACME ")                      # same brand, different case
    progress = api(conn, users["r1"], "rv_progress", cat["cid"], 1)
    assert progress["brand_name"] == "Acme" and progress["edit_count"] == 0
    for bad in ("", "   ", "x" * 201, None):
        with fails("brand name must be"):
            api(conn, users["r1"], "rv_set_brand", cat["cid"], 1, bad)


# ---- dropdown lists -----------------------------------------------------------------------------

def test_lists_start_empty_and_new_values_are_shared_and_deduplicated(conn, users, cat):
    lists = api(conn, users["r1"], "rv_lists")
    assert lists["categories"] == [] and lists["subcategories"] == [] and lists["tags"] == []
    assert [b["name"] for b in lists["brands"]] == ["Acme"]
    kitchen = api(conn, users["r1"], "rv_add_category", " Kitchen ")
    assert api(conn, users["r2"], "rv_add_category", "KITCHEN")["id"] == kitchen["id"]
    cups = api(conn, users["r1"], "rv_add_subcategory", kitchen["id"], "Cups")
    assert api(conn, users["r2"], "rv_add_subcategory", kitchen["id"], "cups")["id"] == cups["id"]
    other = api(conn, users["r1"], "rv_add_category", "Decor")
    assert api(conn, users["r1"], "rv_add_subcategory", other["id"], "Cups")["id"] != cups["id"]   # same name, other category
    lists = api(conn, users["r3"], "rv_lists")
    assert [c["name"] for c in lists["categories"]] == ["Decor", "Kitchen"] and len(lists["subcategories"]) == 2
    with fails("unknown category"):
        api(conn, users["r1"], "rv_add_subcategory", 424242, "X")
    for bad in ("", "  ", "x" * 101, None):
        with fails("must be 1 to 100 characters"):
            api(conn, users["r1"], "rv_add_category", bad)


# ---- finishing ----------------------------------------------------------------------------------

def test_finish_needs_every_page_viewed(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)
    api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 3)
    with fails("pages not viewed yet: 2, 4"):
        api(conn, users["r1"], "rv_finish", cat["cid"], 1)
    assert conn.execute("select level_1_at from review").fetchone()[0] is None


def test_finish_refuses_invalid_products_and_changes_nothing(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    view_all(conn, users["r1"], cat["cid"], 1)
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"product_name": "Renamed"})
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["jar"], None, {"price_amount": None})
    api(conn, users["r1"], "rv_add_product", cat["cid"], 1, 3, {"price_amount": 5})            # no name
    api(conn, users["r1"], "rv_add_product", cat["cid"], 1, 4, {"product_name": "NoPrice"})
    with pytest.raises(RaiseException) as err:
        api(conn, users["r1"], "rv_finish", cat["cid"], 1)
    text = str(err.value)
    assert "fix these products" in text and '"Jar": price is missing' in text
    assert "name is missing" in text and '"NoPrice": price is missing' in text
    assert conn.execute("select product_name from product where product_id = %s", (cat["cup"],)).fetchone()[0] == "Cup"
    assert conn.execute("select count(*) from review_edit").fetchone()[0] == 4              # the draft is intact
    assert conn.execute("select count(*) from review_log where action <> 'claim'").fetchone()[0] == 0
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["jar"], None, {"price_amount": 21})
    api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, None,
        api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 3)["products"][0]["edit_id"])
    api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, None,
        api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 4)["products"][0]["edit_id"])
    assert api(conn, users["r1"], "rv_finish", cat["cid"], 1)["updated"] == 2           # fixed, so it goes through


def test_finish_applies_every_kind_of_edit_and_writes_the_audit_trail(conn, users, cat):
    kitchen = api(conn, users["r1"], "rv_add_category", "Kitchen")
    cups = api(conn, users["r1"], "rv_add_subcategory", kitchen["id"], "Cups")
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["r1"], "rv_set_brand", cat["cid"], 1, "New Brand")
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None,
        {"product_name": "Big Cup", "price_amount": 99.5, "tags": ["B", "a"], "sub_id": cups["id"], "crop": CROP})
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["mug"], None, {"product_name": "Mug", "price_amount": 50})   # no change
    api(conn, users["r1"], "rv_remove_product", cat["cid"], 1, cat["jar"], None)
    api(conn, users["r1"], "rv_add_product", cat["cid"], 1, 3, {"product_name": "Lamp", "price_amount": 10, "tags": ["Light"]})
    api(conn, users["r1"], "rv_add_product", cat["cid"], 1, 1, {"product_name": "Spoon", "price_amount": 3, "crop": {"none": True}})
    view_all(conn, users["r1"], cat["cid"], 1)

    assert api(conn, users["r1"], "rv_finish", cat["cid"], 1) == {"level": 1, "updated": 1, "added": 2, "removed": 1}

    cup = conn.execute("select product_name, price_amount, sub_id, crop, cropped_path from product where product_id = %s", (cat["cup"],)).fetchone()
    assert cup == ("Big Cup", 99.5, cups["id"], {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}, None)      # changed crop clears the cut image
    assert conn.execute("select cropped_path from product where product_id = %s", (cat["mug"],)).fetchone()[0] == "mug.webp"
    assert conn.execute("select count(*) from product where product_id = %s", (cat["jar"],)).fetchone()[0] == 0
    tags = conn.execute("select t.tag_name from product_tag pt join tag t using (tag_id) where pt.product_id = %s order by 1", (cat["cup"],)).fetchall()
    assert [t[0] for t in tags] == ["a", "b"]
    lamp = conn.execute("select extraction_id, position, price_currency, crop from product where product_name = 'Lamp'").fetchone()
    assert lamp == (cat["ex"][3], 1, "INR", None)                                                      # attached to page 3's extraction
    spoon = conn.execute("select extraction_id, position, crop from product where product_name = 'Spoon'").fetchone()
    assert spoon == (cat["ex"][1], 3, {"none": True})                                                  # after the two on page 1
    assert conn.execute("select b.brand_name from catalog c join brand b using (brand_id)").fetchone()[0] == "New Brand"
    review = conn.execute("select reviewer_1_id, level_1_at is not null, level_2_at from review").fetchone()
    assert review == (users["r1"], True, None)
    assert conn.execute("select count(*) from review_edit").fetchone()[0] == 0
    assert conn.execute("select count(*) from review_page_seen").fetchone()[0] == 0

    log = conn.execute("select action, product_id, detail from review_log where catalog_id = %s order by log_id", (cat["cid"],)).fetchall()
    actions = [a for a, _, _ in log]
    assert actions.count("update") == 1 and actions.count("add") == 2 and actions.count("remove") == 1
    assert actions.count("brand") == 1 and actions[-1] == "finish" and "claim" in actions
    update = next(d for a, _, d in log if a == "update")
    assert set(update["before"]) == set(update["after"]) == {"product_name", "price_amount", "tags", "sub_id", "crop"}
    assert update["before"]["product_name"] == "Cup" and update["after"]["product_name"] == "Big Cup"
    assert next(d for a, _, d in log if a == "remove")["before"]["product_name"] == "Jar"
    assert next(d for a, _, d in log if a == "brand") == {"before": "Acme", "after": "New Brand"}
    assert conn.execute("select count(distinct user_id) from review_log where catalog_id = %s", (cat["cid"],)).fetchone()[0] == 1


def test_a_finished_level_is_closed_to_its_reviewer(conn, users, cat):
    level_1_done(conn, users, cat["cid"])
    for call in (lambda: api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1),
                 lambda: api(conn, users["r1"], "rv_finish", cat["cid"], 1),
                 lambda: api(conn, users["r1"], "rv_release", cat["cid"], 1)):
        with fails("already finished"):
            call()


def test_the_audit_log_cannot_be_changed(conn, users, cat):
    level_1_done(conn, users, cat["cid"])
    with pytest.raises(RaiseException, match="append-only"):
        conn.execute("update review_log set action = 'x'")
    with pytest.raises(RaiseException, match="append-only"):
        conn.execute("delete from review_log")


# ---- level 2 ------------------------------------------------------------------------------------

def test_level_2_waits_for_level_1_and_needs_a_different_person(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    with fails("Level 1 is being done by another reviewer"):
        api(conn, users["r2"], "rv_open", cat["cid"])
    view_all(conn, users["r1"], cat["cid"], 1)
    api(conn, users["r1"], "rv_finish", cat["cid"], 1)
    with fails("different person than Level 1"):
        api(conn, users["r1"], "rv_open", cat["cid"])
    listed = {c["catalog_name"]: c for c in api(conn, users["r1"], "rv_list_catalogs")}["Acme"]
    assert listed["state"] == "level_2_open" and listed["can_open"] is False
    assert api(conn, users["r2"], "rv_open", cat["cid"])["level"] == 2


def test_level_2_sees_level_1_results_and_can_change_them(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"product_name": "Cup L1", "crop": CROP})
    view_all(conn, users["r1"], cat["cid"], 1)
    api(conn, users["r1"], "rv_finish", cat["cid"], 1)

    assert api(conn, users["r2"], "rv_open", cat["cid"])["pages_seen"] == 0                # a full pass again
    cup = api(conn, users["r2"], "rv_get_page", cat["cid"], 2, 1)["products"][0]
    assert cup["product_name"] == "Cup L1" and cup["crop"] == {"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.4}   # the screen can draw it
    api(conn, users["r2"], "rv_save_product", cat["cid"], 2, cat["cup"], None, {"product_name": "Cup L2"})
    for p in (2, 3, 4):
        api(conn, users["r2"], "rv_get_page", cat["cid"], 2, p)
    assert api(conn, users["r2"], "rv_finish", cat["cid"], 2)["updated"] == 1
    assert conn.execute("select product_name from product where product_id = %s", (cat["cup"],)).fetchone()[0] == "Cup L2"
    row = conn.execute("select reviewer_1_id, reviewer_2_id, level_2_at is not null from review").fetchone()
    assert row == (users["r1"], users["r2"], True)
    levels = [lv for (lv,) in conn.execute("select level from review_log where action = 'update' order by log_id")]
    assert levels == [1, 2]
    state = {c["catalog_name"]: c for c in api(conn, users["r3"], "rv_list_catalogs")}["Acme"]
    assert state["state"] == "done" and state["can_open"] is False
    with fails("finished both review levels"):
        api(conn, users["r3"], "rv_open", cat["cid"])


def test_level_1_drafts_do_not_leak_into_level_2(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    view_all(conn, users["r1"], cat["cid"], 1)
    api(conn, users["r1"], "rv_finish", cat["cid"], 1)
    api(conn, users["r2"], "rv_open", cat["cid"])
    api(conn, users["r2"], "rv_save_product", cat["cid"], 2, cat["cup"], None, {"product_name": "Draft L2"})
    assert api(conn, users["r2"], "rv_progress", cat["cid"], 2)["edit_count"] == 1
    assert conn.execute("select count(*) from review_edit where level = 1").fetchone()[0] == 0


# ---- releasing ----------------------------------------------------------------------------------

def test_releasing_discards_the_draft_and_lets_someone_else_take_over(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"product_name": "Draft"})
    api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)
    with fails("only the reviewer who opened it, or an admin", InsufficientPrivilege):
        api(conn, users["r2"], "rv_release", cat["cid"], 1)
    api(conn, users["r1"], "rv_release", cat["cid"], 1)
    assert conn.execute("select reviewer_1_id from review").fetchone()[0] is None
    assert conn.execute("select count(*) from review_edit").fetchone()[0] == 0 == conn.execute("select count(*) from review_page_seen").fetchone()[0]
    assert api(conn, users["r2"], "rv_open", cat["cid"])["pages_seen"] == 0
    with fails("have not opened level 1", InsufficientPrivilege):
        api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)
    release = conn.execute("select detail from review_log where action = 'release'").fetchone()[0]
    assert release["discarded_edits"] == 1 and release["held_by"] == str(users["r1"])


def test_an_admin_can_release_someone_elses_level(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["admin"], "rv_release", cat["cid"], 1)
    assert api(conn, users["r2"], "rv_open", cat["cid"])["level"] == 1


def test_release_refuses_levels_that_are_not_open(conn, users, cat):
    with fails("not open"):
        api(conn, users["r1"], "rv_release", cat["cid"], 1)
    api(conn, users["r1"], "rv_open", cat["cid"])
    with fails("level 2 of this catalogue is not open"):
        api(conn, users["r1"], "rv_release", cat["cid"], 2)
    with fails("level must be 1 or 2"):
        api(conn, users["r1"], "rv_release", cat["cid"], 0)


def test_after_a_release_the_person_who_did_level_1_is_still_blocked_from_level_2(conn, users, cat):
    level_1_done(conn, users, cat["cid"])
    api(conn, users["r2"], "rv_open", cat["cid"])
    api(conn, users["r2"], "rv_release", cat["cid"], 2)
    with fails("different person than Level 1"):
        api(conn, users["r1"], "rv_open", cat["cid"])
    assert api(conn, users["r3"], "rv_open", cat["cid"])["level"] == 2


# ---- users and admin ----------------------------------------------------------------------------

def test_only_admins_manage_users_and_read_the_audit(conn, users, cat):
    for name, args in [("admin_add_user", ("x@t.example", "reviewer")), ("admin_list_users", ()),
                       ("admin_set_user", (users["r2"], "admin", True)), ("rv_audit", (cat["cid"],))]:
        with fails("admin only", InsufficientPrivilege):
            api(conn, users["r1"], name, *args)
    assert len(api(conn, users["admin"], "admin_list_users")) == 5


def test_sign_up_is_invite_only_so_a_role_needs_an_existing_login(conn, users):
    with fails("no sign-in account for nobody@t.example"):
        api(conn, users["admin"], "admin_add_user", "nobody@t.example", "reviewer")
    stranger = conn.execute("select email from auth.users where id = %s", (users["stranger"],)).fetchone()[0]
    with fails("role must be reviewer or admin"):
        api(conn, users["admin"], "admin_add_user", stranger, "owner")
    added = api(conn, users["admin"], "admin_add_user", f" {stranger.upper()} ", "reviewer")
    assert added["role"] == "reviewer" and added["email"] == stranger
    assert api(conn, users["stranger"], "rv_me")["role"] == "reviewer"                    # now they can sign in and work
    with fails("already a user"):
        api(conn, users["admin"], "admin_add_user", stranger, "reviewer")


def test_changing_roles_and_deactivating_users(conn, users, cat):
    api(conn, users["admin"], "admin_set_user", users["r2"], "admin", True)
    assert api(conn, users["r2"], "admin_list_users")
    api(conn, users["admin"], "admin_set_user", users["r2"], "reviewer", False)
    with fails("not an active user", InsufficientPrivilege):
        api(conn, users["r2"], "rv_me")
    with fails("unknown user"):
        api(conn, users["admin"], "admin_set_user", uuid.uuid4(), "reviewer", True)
    with fails("role must be reviewer or admin"):
        api(conn, users["admin"], "admin_set_user", users["r1"], "boss", True)
    with fails("active must be true or false"):
        api(conn, users["admin"], "admin_set_user", users["r1"], "reviewer", None)


def test_there_is_always_one_active_admin(conn, users):
    with fails("always be one active admin"):
        api(conn, users["admin"], "admin_set_user", users["admin"], "reviewer", True)
    with fails("always be one active admin"):
        api(conn, users["admin"], "admin_set_user", users["admin"], "admin", False)
    api(conn, users["admin"], "admin_set_user", users["r1"], "admin", True)               # a second admin
    api(conn, users["admin"], "admin_set_user", users["admin"], "reviewer", True)         # now the first may step down
    assert api(conn, users["r1"], "rv_me")["role"] == "admin"


def test_a_deactivated_reviewer_loses_access_but_an_admin_can_free_their_catalogue(conn, users, cat):
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["admin"], "admin_set_user", users["r1"], "reviewer", False)
    with fails("not an active user", InsufficientPrivilege):
        api(conn, users["r1"], "rv_get_page", cat["cid"], 1, 1)
    api(conn, users["admin"], "rv_release", cat["cid"], 1)
    assert api(conn, users["r2"], "rv_open", cat["cid"])["level"] == 1


def test_the_audit_names_who_did_what(conn, users, cat):
    kitchen = api(conn, users["r1"], "rv_add_category", "Kitchen")
    api(conn, users["r1"], "rv_open", cat["cid"])
    api(conn, users["r1"], "rv_save_product", cat["cid"], 1, cat["cup"], None, {"product_name": "Cup 2"})
    view_all(conn, users["r1"], cat["cid"], 1)
    api(conn, users["r1"], "rv_finish", cat["cid"], 1)
    api(conn, users["r2"], "rv_open", cat["cid"])
    rows = api(conn, users["admin"], "rv_audit", cat["cid"])
    assert [(r["action"], r["by"], r["level"]) for r in rows] == [
        ("claim", "r1@t.example", 1), ("update", "r1@t.example", 1), ("finish", "r1@t.example", 1),
        ("claim", "r2@t.example", 2)]
    assert api(conn, users["admin"], "rv_audit", 424242) == []
    shared = conn.execute("select action, user_id from review_log where catalog_id is null").fetchall()
    assert shared == [("add_category", users["r1"])] and kitchen["name"] == "Kitchen"


# ---- storage policy -----------------------------------------------------------------------------

def test_only_active_users_can_read_catalogue_pdfs(conn, users):
    """Runs on the plain-Postgres stand-in for storage.objects; on a real Supabase project the policy is
    created the same way but that table is not ours to fill, so the check is skipped."""
    columns = conn.execute("select count(*) from information_schema.columns where table_schema = 'storage' and table_name = 'objects'").fetchone()[0]
    if not columns or columns > 3:
        pytest.skip("storage.objects is not the test stand-in")
    conn.execute("delete from storage.objects")
    conn.execute("insert into storage.objects (bucket_id, name) values ('catalogs', 'pdfs/a.pdf'), ('other', 'x.pdf')")
    try:
        def visible(who):
            with as_user(conn, who):
                return [r[0] for r in conn.execute("select name from storage.objects order by name")]
        assert visible(users["r1"]) == ["pdfs/a.pdf"]
        assert visible(users["admin"]) == ["pdfs/a.pdf"]
        assert visible(users["stranger"]) == [] and visible(users["gone"]) == []
        assert visible(None) == []
    finally:
        conn.execute("delete from storage.objects")
