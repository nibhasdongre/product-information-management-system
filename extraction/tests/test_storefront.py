"""Storefront backend: customers, the wishlist, and the search/filter functions the storefront calls.
Every rule is checked by calling the database functions the way the storefront will, as different
signed-in people (customer, reviewer, inactive, no role) and as anon. Helpers come from test_review.

The shop below has seven products, of which four are visible: a product shows only if its catalogue has
finished Level 2, it has a price, and it comes from the extraction the reviewer saw for its page."""
import json
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege, RaiseException

from test_review import api, as_user, fails, open_schema_to_roles  # noqa: F401  (fixture is used automatically)

ARRAY_TYPES = {"brand_ids": "bigint", "category_ids": "bigint", "sub_ids": "bigint", "tag_names": "text"}
VISIBLE = ["Steel Bottle", "Bamboo Pen", "Notebook", "Gift Hamper"]       # in the shop's default order


def named(conn, who, fn, **kw):
    """Call a function with named arguments (lists become typed arrays), as PostgREST does."""
    def literal(key, value):
        if isinstance(value, list):
            kind = sql.SQL(ARRAY_TYPES[key])
            if not value:
                return sql.SQL("'{{}}'::{}[]").format(kind)
            return sql.SQL("ARRAY[{}]::{}[]").format(sql.SQL(", ").join(sql.Literal(v) for v in value), kind)
        return sql.Literal(value)
    args = sql.SQL(", ").join(sql.SQL("{} => {}").format(sql.SQL(k), literal(k, v)) for k, v in kw.items())
    query = sql.SQL("select set_config('request.jwt.claim.sub', {who}, false), set_config('role', {role}, false); "
                    "select {fn}({args}); reset role").format(
        who=sql.Literal(str(who or "")), role=sql.Literal("anon" if who is None else "authenticated"),
        fn=sql.SQL(fn), args=args)
    cur = conn.execute(query)
    cur.nextset()
    return cur.fetchone()[0]


def search(conn, who, **kw):
    return named(conn, who, "sf_search", **kw)


def names(result):
    return [item["product_name"] for item in result["items"]]


@pytest.fixture
def people(conn):
    ids = {k: uuid.uuid4() for k in ("c1", "c2", "cgone", "staff", "r2", "sgone", "stranger")}
    rows = ", ".join(f"('{u}', '{k}-{u.hex[:8]}@t.example')" for k, u in ids.items())
    conn.execute(f"""
        insert into auth.users (id, email) values {rows};
        insert into customer (user_id, email, company, active) values
          ('{ids['c1']}', 'c1@t.example', 'Acme Corp', true), ('{ids['c2']}', 'c2@t.example', null, true),
          ('{ids['cgone']}', 'cgone@t.example', null, false);
        insert into app_user (user_id, email, role, active) values
          ('{ids['staff']}', 'staff@t.example', 'reviewer', true), ('{ids['r2']}', 'r2@t.example', 'reviewer', true),
          ('{ids['sgone']}', 'sgone@t.example', 'reviewer', false);
    """)
    yield ids
    conn.execute("delete from wishlist_item; delete from review")
    conn.execute("delete from auth.users where id = any(%s)", (list(ids.values()),))


@pytest.fixture
def shop(conn, people):
    s, r2 = people["staff"], people["r2"]
    conn.execute(f"""
        insert into brand (brand_id, brand_name) overriding system value values (1, 'Acme'), (2, 'Beta'), (3, 'Gamma');
        insert into category (category_id, category_name) overriding system value values (1, 'Kitchen'), (2, 'Office'), (3, 'Gifting');
        insert into subcategory (sub_id, subcat_name, category_id) overriding system value values
          (1, 'Drinkware', 1), (2, 'Stationery', 2), (3, 'Hampers', 3);
        insert into catalog (catalog_id, catalog_name, brand_id, file_sha256, storage_path, page_count) overriding system value values
          (1, 'Acme 2026', 1, repeat('a', 64), 'pdfs/a.pdf', 2), (2, 'Beta 2026', 2, repeat('b', 64), 'pdfs/b.pdf', 1),
          (3, 'Gamma 2026', 3, repeat('c', 64), 'pdfs/c.pdf', 1);
        insert into page_extractions (extraction_id, catalog_id, page, model, status, page_type, dpi, model_dpi) overriding system value values
          (1, 1, 1, 'm', 'ok', 'product', 72, 72), (2, 1, 2, 'm', 'ok', 'product', 72, 72),
          (3, 2, 1, 'm', 'ok', 'product', 72, 72), (4, 3, 1, 'm', 'ok', 'product', 72, 72),
          (5, 1, 1, 'm2', 'ok', 'product', 72, 72);
        insert into product (product_id, extraction_id, position, product_name, product_description, price_amount, sub_id, cropped_path) overriding system value values
          (1, 1, 1, 'Steel Bottle', 'Insulated bottle keeps drinks cold', 500, 1, 'products/aaa'),
          (2, 1, 2, 'Bamboo Pen', 'Bamboo ballpoint pen', 80, 2, null),
          (3, 2, 1, 'Notebook', 'Recycled paper notebook', 150, 2, 'products/ccc'),
          (4, 2, 2, 'Unpriced Thing', null, null, 2, null),
          (5, 3, 1, 'Hidden Mug', null, 60, 1, null),
          (6, 4, 1, 'Gift Hamper', 'Festive hamper for clients', 1500, 3, 'products/hhh'),
          (7, 5, 1, 'Old Duplicate', null, 10, 1, null);
        insert into tag (tag_id, tag_name) overriding system value values (1, 'eco'), (2, 'steel'), (3, 'paper'), (4, 'festive');
        insert into product_tag (product_id, tag_id) values (1, 1), (1, 2), (2, 1), (3, 3), (6, 4), (5, 1);
        insert into review (catalog_id, reviewer_1_id, level_1_at, reviewer_2_id, level_2_at) values
          (1, '{s}', now(), '{r2}', now()), (3, '{s}', now(), '{r2}', now());
        insert into review (catalog_id, reviewer_1_id, level_1_at) values (2, '{s}', now());
    """)
    return {"bottle": 1, "pen": 2, "notebook": 3, "unpriced": 4, "mug": 5, "hamper": 6, "duplicate": 7}


# ---- who may call what -------------------------------------------------------------------------

CALLS = [("sf_me", ()), ("sf_filters", ()), ("sf_search", ()), ("sf_get_product", (1,)), ("sf_wishlist", ()),
         ("sf_wishlist_add", (1,)), ("sf_wishlist_remove", (1,)), ("sf_wishlist_update", (1, 2, "x"))]


def test_anon_cannot_call_any_function(conn, people, shop):
    for name, args in CALLS:
        with fails("permission denied", InsufficientPrivilege):
            api(conn, None, name, *args)


def test_internal_helpers_and_the_view_are_off_limits(conn, people, shop):
    with fails("permission denied", InsufficientPrivilege):
        api(conn, people["c1"], "sfx_actor")
    with as_user(conn, people["c1"]), pytest.raises(InsufficientPrivilege):
        conn.execute("select * from sfx_product")


@pytest.mark.parametrize("who", ["stranger", "cgone", "sgone"])
def test_people_without_an_active_account_can_do_nothing(conn, people, shop, who):
    for name, args in CALLS:
        with fails("not an active user", InsufficientPrivilege):
            api(conn, people[who], name, *args)


def test_customers_and_staff_can_both_use_the_storefront(conn, people, shop):
    me = api(conn, people["c1"], "sf_me")
    assert (me["kind"], me["email"], me["company"], me["wishlist_count"]) == ("customer", "c1@t.example", "Acme Corp", 0)
    staff = api(conn, people["staff"], "sf_me")
    assert (staff["kind"], staff["role"]) == ("staff", "reviewer")
    assert names(search(conn, people["staff"])) == VISIBLE


def test_a_customer_cannot_reach_the_review_side(conn, people, shop):
    for name, args in [("rv_me", ()), ("rv_list_catalogs", ()), ("rv_lists", ()), ("rv_open", (1,)), ("admin_list_users", ())]:
        with fails("not an active user", InsufficientPrivilege):
            api(conn, people["c1"], name, *args)
    assert api(conn, people["c1"], "is_active_user") is False        # so the PDF storage policy refuses them too


def test_the_tables_cannot_be_read_or_written_directly(conn, people, shop):
    api(conn, people["c1"], "sf_wishlist_add", 1)
    for table in ("customer", "wishlist_item", "product", "review"):
        with as_user(conn, people["c1"]):
            try:
                rows = conn.execute(f"select count(*) from {table}").fetchone()[0]
            except InsufficientPrivilege:
                rows = 0
        assert rows == 0, table
    with as_user(conn, people["c1"]):
        for write in ("delete from wishlist_item", "update customer set active = false", "update product set product_name = 'x'"):
            try:
                changed = conn.execute(write).rowcount
            except InsufficientPrivilege:
                changed = 0
            assert changed == 0, write
    assert conn.execute("select (select count(*) from wishlist_item), (select count(*) from customer where active)").fetchone() == (1, 2)


def test_new_tables_have_row_level_security_on(conn):
    off = conn.execute("select c.relname from pg_class c where c.relnamespace = current_schema()::regnamespace "
                       "and c.relname in ('customer','wishlist_item') and not c.relrowsecurity").fetchall()
    assert off == []


# ---- what is visible ---------------------------------------------------------------------------

def test_only_reviewed_priced_products_from_the_chosen_extraction_show(conn, people, shop):
    result = search(conn, people["c1"])
    assert names(result) == VISIBLE and result["total"] == 4
    hidden = {"Unpriced Thing", "Hidden Mug", "Old Duplicate"}
    assert not hidden & set(names(result))


def test_a_card_carries_what_the_grid_needs(conn, people, shop):
    items = {i["product_name"]: i for i in search(conn, people["c1"])["items"]}
    bottle = items["Steel Bottle"]
    assert bottle["price_amount"] == 500 and bottle["price_currency"] == "INR"
    assert (bottle["brand_name"], bottle["category_name"], bottle["subcat_name"]) == ("Acme", "Kitchen", "Drinkware")
    assert bottle["tags"] == ["eco", "steel"] and bottle["image_path"] == "products/aaa" and bottle["in_wishlist"] is False
    assert items["Bamboo Pen"]["image_path"] is None             # no published image: the page shows a placeholder
    assert "product_description" not in bottle                    # the grid card stays small


def test_a_catalogue_that_has_not_finished_level_2_stays_hidden_until_it_does(conn, people, shop):
    conn.execute("update review set level_2_at = null where catalog_id = 3")
    assert names(search(conn, people["c1"])) == ["Steel Bottle", "Bamboo Pen", "Notebook"]
    conn.execute("update review set reviewer_2_id = %s, level_2_at = now() where catalog_id = 2", (people["r2"],))
    assert "Hidden Mug" in names(search(conn, people["c1"]))


# ---- search ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("bottle", ["Steel Bottle"]),
    ("BOTTLE", ["Steel Bottle"]),
    ("  bottle  ", ["Steel Bottle"]),
    ("steel", ["Steel Bottle"]),                       # a tag
    ("acme", ["Steel Bottle", "Bamboo Pen", "Notebook"]),     # the brand
    ("stationery", ["Bamboo Pen", "Notebook"]),        # the subcategory
    ("kitchen", ["Steel Bottle"]),                     # the category
    ("recycled paper", ["Notebook"]),                  # every word must match
    ("paper bottle", []),
    ("festive clients", ["Gift Hamper"]),
    ("nothing like this", []),
    ("%", []), ("_", []), ("\\", []), ("bott%", []),   # wildcard characters are plain text
    ("'; drop table product; --", []),
    ("", VISIBLE), (None, VISIBLE),
])
def test_search_text(conn, people, shop, text, expected):
    assert names(search(conn, people["c1"], search_text=text)) == expected
    assert conn.execute("select count(*) from product").fetchone()[0] == 7


# ---- filters -----------------------------------------------------------------------------------

@pytest.mark.parametrize("kw, expected", [
    ({"brand_ids": [3]}, ["Gift Hamper"]),
    ({"brand_ids": [1, 3]}, VISIBLE),
    ({"brand_ids": [2]}, []),                                            # Beta has not finished Level 2
    ({"category_ids": [2]}, ["Bamboo Pen", "Notebook"]),
    ({"sub_ids": [1, 3]}, ["Steel Bottle", "Gift Hamper"]),
    ({"tag_names": ["eco"]}, ["Steel Bottle", "Bamboo Pen"]),
    ({"tag_names": ["ECO ", "festive"]}, ["Steel Bottle", "Bamboo Pen", "Gift Hamper"]),     # any of them
    ({"tag_names": ["unknown"]}, []),
    ({"price_min": 100}, ["Steel Bottle", "Notebook", "Gift Hamper"]),
    ({"price_max": 150}, ["Bamboo Pen", "Notebook"]),
    ({"price_min": 150, "price_max": 150}, ["Notebook"]),
    ({"price_min": 80, "price_max": 500, "tag_names": ["eco"]}, ["Steel Bottle", "Bamboo Pen"]),
    ({"category_ids": [2], "tag_names": ["eco"]}, ["Bamboo Pen"]),      # across filters, all must match
    ({"category_ids": [1], "sub_ids": [2]}, ["Steel Bottle", "Bamboo Pen", "Notebook"]),   # category and subcategory: either
    ({"category_ids": [1], "sub_ids": [3], "tag_names": ["eco"]}, ["Steel Bottle"]),
    ({"search_text": "pen", "brand_ids": [1], "price_max": 100}, ["Bamboo Pen"]),
    ({"brand_ids": [], "category_ids": [], "sub_ids": [], "tag_names": []}, VISIBLE),
])
def test_filters(conn, people, shop, kw, expected):
    assert names(search(conn, people["c1"], **kw)) == expected


@pytest.mark.parametrize("sort, expected", [
    ("featured", VISIBLE),
    ("price_asc", ["Bamboo Pen", "Notebook", "Steel Bottle", "Gift Hamper"]),
    ("price_desc", ["Gift Hamper", "Steel Bottle", "Notebook", "Bamboo Pen"]),
    ("name", ["Bamboo Pen", "Gift Hamper", "Notebook", "Steel Bottle"]),
])
def test_sorting(conn, people, shop, sort, expected):
    assert names(search(conn, people["c1"], sort_by=sort)) == expected


def test_paging_keeps_the_total_and_the_order(conn, people, shop):
    first = search(conn, people["c1"], page_size=3)
    second = search(conn, people["c1"], page_size=3, page_no=2)
    beyond = search(conn, people["c1"], page_size=3, page_no=3)
    assert (first["total"], second["total"], beyond["total"]) == (4, 4, 4)
    assert names(first) + names(second) == VISIBLE and names(beyond) == []
    assert (first["page"], first["page_size"]) == (1, 3)


@pytest.mark.parametrize("kw, message", [
    ({"search_text": "x" * 201}, "too long"),
    ({"search_text": " ".join("w" * 11)}, "too many search words"),
    ({"sort_by": "random"}, "sort_by must be"),
    ({"page_no": 0}, "page_no must be"),
    ({"page_size": 0}, "page_size must be"),
    ({"page_size": 101}, "page_size must be"),
    ({"price_min": 10, "price_max": 5}, "price_min cannot"),
    ({"brand_ids": list(range(101))}, "too many filter values"),
    ({"tag_names": [str(i) for i in range(101)]}, "too many filter values"),
])
def test_bad_search_requests_are_refused(conn, people, shop, kw, message):
    with fails(message):
        search(conn, people["c1"], **kw)


def test_the_filter_panel_lists_what_exists_with_counts(conn, people, shop):
    f = api(conn, people["c1"], "sf_filters")
    assert f["total"] == 4 and (f["price_min"], f["price_max"]) == (80, 1500)
    assert {(b["name"], b["count"]) for b in f["brands"]} == {("Acme", 3), ("Gamma", 1)}      # Beta is hidden
    office = next(c for c in f["categories"] if c["name"] == "Office")
    assert office["count"] == 2 and [(s["name"], s["count"]) for s in office["subcategories"]] == [("Stationery", 2)]
    assert {(t["name"], t["count"]) for t in f["tags"]} == {("eco", 2), ("steel", 1), ("paper", 1), ("festive", 1)}
    assert "Gifting" in {c["name"] for c in f["categories"]} and "Kitchen" in {c["name"] for c in f["categories"]}


def test_the_filter_panel_is_empty_before_anything_is_reviewed(conn, people):
    f = api(conn, people["c1"], "sf_filters")
    assert f["total"] == 0 and f["price_min"] is None and f["brands"] == [] and f["categories"] == [] and f["tags"] == []
    assert search(conn, people["c1"]) == {"total": 0, "page": 1, "page_size": 24, "items": []}


# ---- one product -------------------------------------------------------------------------------

def test_the_detail_view_has_every_field(conn, people, shop):
    p = api(conn, people["c1"], "sf_get_product", shop["notebook"])
    assert p["product_description"] == "Recycled paper notebook" and p["price_amount"] == 150
    for field in ("tax_note", "uom", "weight", "weight_unit", "variant"):
        assert field in p
    assert p["in_wishlist"] is False and "quantity" not in p


@pytest.mark.parametrize("which", ["unpriced", "mug", "duplicate"])
def test_a_hidden_product_cannot_be_opened(conn, people, shop, which):
    with fails("product not found"):
        api(conn, people["c1"], "sf_get_product", shop[which])
    with fails("product not found"):
        api(conn, people["c1"], "sf_get_product", 99999)


# ---- wishlist ----------------------------------------------------------------------------------

def test_adding_and_removing_wishlist_items(conn, people, shop):
    c1 = people["c1"]
    assert api(conn, c1, "sf_wishlist_add", shop["bottle"]) == {"count": 1}
    assert api(conn, c1, "sf_wishlist_add", shop["bottle"]) == {"count": 1}            # adding twice changes nothing
    assert api(conn, c1, "sf_wishlist_add", shop["hamper"]) == {"count": 2}
    items = api(conn, c1, "sf_wishlist")["items"]
    assert [i["product_name"] for i in items] == ["Steel Bottle", "Gift Hamper"]       # in the order they were added
    assert all(i["in_wishlist"] and i["quantity"] is None and i["customization"] is None for i in items)
    assert api(conn, c1, "sf_me")["wishlist_count"] == 2
    assert api(conn, c1, "sf_wishlist_remove", shop["bottle"]) == {"count": 1}
    assert api(conn, c1, "sf_wishlist_remove", shop["bottle"]) == {"count": 1}         # removing twice is fine
    assert [i["product_name"] for i in api(conn, c1, "sf_wishlist")["items"]] == ["Gift Hamper"]


@pytest.mark.parametrize("which", ["unpriced", "mug", "duplicate"])
def test_hidden_products_cannot_be_added(conn, people, shop, which):
    with fails("product not found"):
        api(conn, people["c1"], "sf_wishlist_add", shop[which])
    with fails("product not found"):
        api(conn, people["c1"], "sf_wishlist_add", 99999)
    assert api(conn, people["c1"], "sf_wishlist") == {"items": []}


def test_each_person_has_their_own_wishlist(conn, people, shop):
    api(conn, people["c1"], "sf_wishlist_add", shop["bottle"])
    api(conn, people["c2"], "sf_wishlist_add", shop["pen"])
    api(conn, people["staff"], "sf_wishlist_add", shop["notebook"])
    for who, expected in (("c1", "Steel Bottle"), ("c2", "Bamboo Pen"), ("staff", "Notebook")):
        assert [i["product_name"] for i in api(conn, people[who], "sf_wishlist")["items"]] == [expected]
    api(conn, people["c2"], "sf_wishlist_remove", shop["bottle"])                      # not theirs: nothing happens
    assert len(api(conn, people["c1"], "sf_wishlist")["items"]) == 1
    with fails("not in your wishlist"):
        api(conn, people["c2"], "sf_wishlist_update", shop["bottle"], 5, "x")


def test_quantity_and_customization_are_kept_and_can_be_cleared(conn, people, shop):
    c1 = people["c1"]
    api(conn, c1, "sf_wishlist_add", shop["bottle"])
    item = api(conn, c1, "sf_wishlist_update", shop["bottle"], 250, "  Logo on the lid, matte black  ")
    assert (item["quantity"], item["customization"]) == (250, "Logo on the lid, matte black")
    listed = api(conn, c1, "sf_wishlist")["items"][0]
    assert (listed["quantity"], listed["customization"], listed["product_description"]) == (250, "Logo on the lid, matte black", "Insulated bottle keeps drinks cold")
    cleared = api(conn, c1, "sf_wishlist_update", shop["bottle"], None, "   ")
    assert cleared["quantity"] is None and cleared["customization"] is None
    assert api(conn, c1, "sf_wishlist_update", shop["bottle"], 1, None)["quantity"] == 1
    assert api(conn, c1, "sf_wishlist_update", shop["bottle"], 1000000, "x" * 1000)["quantity"] == 1000000


@pytest.mark.parametrize("qty, note, message", [
    (0, "x", "quantity must be"), (-5, "x", "quantity must be"), (1000001, "x", "quantity must be"),
    (5, "x" * 1001, "note is too long"),
])
def test_bad_quantities_and_notes_are_refused(conn, people, shop, qty, note, message):
    api(conn, people["c1"], "sf_wishlist_add", shop["bottle"])
    with fails(message):
        api(conn, people["c1"], "sf_wishlist_update", shop["bottle"], qty, note)
    assert api(conn, people["c1"], "sf_wishlist")["items"][0]["quantity"] is None       # nothing changed


def test_updating_an_item_that_was_never_added_is_refused(conn, people, shop):
    with fails("not in your wishlist"):
        api(conn, people["c1"], "sf_wishlist_update", shop["bottle"], 5, "x")


def test_search_and_detail_show_which_products_are_in_the_wishlist(conn, people, shop):
    api(conn, people["c1"], "sf_wishlist_add", shop["pen"])
    api(conn, people["c2"], "sf_wishlist_add", shop["hamper"])
    flags = {i["product_name"]: i["in_wishlist"] for i in search(conn, people["c1"])["items"]}
    assert flags == {"Steel Bottle": False, "Bamboo Pen": True, "Notebook": False, "Gift Hamper": False}
    assert api(conn, people["c1"], "sf_get_product", shop["pen"])["in_wishlist"] is True
    assert api(conn, people["c1"], "sf_get_product", shop["hamper"])["in_wishlist"] is False


def test_a_product_that_becomes_hidden_drops_out_of_the_wishlist_and_the_count(conn, people, shop):
    api(conn, people["c1"], "sf_wishlist_add", shop["bottle"])
    api(conn, people["c1"], "sf_wishlist_add", shop["hamper"])
    conn.execute("update review set level_2_at = null where catalog_id = 3")
    assert [i["product_name"] for i in api(conn, people["c1"], "sf_wishlist")["items"]] == ["Steel Bottle"]
    assert api(conn, people["c1"], "sf_me")["wishlist_count"] == 1
    conn.execute("update review set level_2_at = now() where catalog_id = 3")
    assert len(api(conn, people["c1"], "sf_wishlist")["items"]) == 2           # it returns with its quantity and note intact


def test_wishlist_rows_go_when_the_person_or_the_product_goes(conn, people, shop):
    api(conn, people["c1"], "sf_wishlist_add", shop["bottle"])
    api(conn, people["c2"], "sf_wishlist_add", shop["bottle"])
    api(conn, people["c2"], "sf_wishlist_add", shop["pen"])
    conn.execute("delete from auth.users where id = %s", (people["c1"],))
    assert conn.execute("select count(*) from wishlist_item").fetchone()[0] == 2
    assert conn.execute("select count(*) from customer where user_id = %s", (people["c1"],)).fetchone()[0] == 0
    conn.execute("delete from product_tag where product_id = 2; delete from product where product_id = 2")
    assert conn.execute("select count(*) from wishlist_item").fetchone()[0] == 1


def test_a_deactivated_customer_is_locked_out_at_once(conn, people, shop):
    api(conn, people["c1"], "sf_wishlist_add", shop["bottle"])
    conn.execute("update customer set active = false where user_id = %s", (people["c1"],))
    with fails("not an active user", InsufficientPrivilege):
        api(conn, people["c1"], "sf_wishlist")
    conn.execute("update customer set active = true where user_id = %s", (people["c1"],))
    assert len(api(conn, people["c1"], "sf_wishlist")["items"]) == 1
