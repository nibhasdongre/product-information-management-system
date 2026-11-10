"""Building product rows from raw model output. No database needed."""
from decimal import Decimal

import pytest

from pipeline.records import parse_weight, product_row
from pipeline.repo import brand_from_key


@pytest.mark.parametrize("text, expected", [
    ("247 gms", (Decimal("247"), "gms")),
    ("500ml", (Decimal("500"), "ml")),
    ("1.5 KG", (Decimal("1.5"), "kg")),
    ("1,200 g", (Decimal("1200"), "g")),
    ("15x20 cm", (None, None)),
    ("1,5 kg", (None, None)),
    ("about a pound", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
    ("99999999999 g", (None, None)),
])
def test_parse_weight(text, expected):
    assert parse_weight(text) == expected


def test_product_row_defaults_and_guards():
    row = product_row({"product_name": " Mug ", "price_amount": True, "confidence": 1.5,
                       "price_currency": "", "category": " ", "subcategory": "Cups"}, 3)
    assert row["position"] == 3 and row["product_name"] == "Mug"
    assert row["price_amount"] is None            # a boolean is not a price
    assert row["price_currency"] == "INR"
    assert row["confidence"] is None and row["extraction_flags"] == ["confidence_out_of_range"]
    assert row["category"] is None and row["subcategory"] == "Cups"


@pytest.mark.parametrize("key, brand", [("Jack_Jones", "Jack Jones"), ("Turtle", "Turtle"), ("_Rare__Rabbit_", "Rare  Rabbit")])
def test_brand_comes_from_the_file_name(key, brand):
    assert brand_from_key(key) == brand
