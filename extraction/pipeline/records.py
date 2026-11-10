"""Turn one model product (the raw JSON) into a `product` table row.

The raw output is kept untouched in extraction_data; this builds the reviewer-editable copy.
Anything the schema cannot hold cleanly is left null here and stays recoverable from the raw
output, so a malformed value never fails the whole page."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

_WEIGHT = re.compile(
    r"^\s*(\d+(?:[.,]\d+)*)\s*(kgs?|g|gms?|grams?|mg|ltrs?|litres?|liters?|l|ml|oz|lbs?)\b", re.IGNORECASE)
_THOUSANDS = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_MAX_NUMERIC = 10 ** 9


def parse_weight(text: Any) -> tuple[Decimal | None, str | None]:
    """'247 gms' -> (247, 'gms'). Anything not clearly a number plus a known unit -> (None, None)."""
    if not isinstance(text, str):
        return None, None
    match = _WEIGHT.match(text)
    if not match:
        return None, None
    number, unit = match.group(1), match.group(2).lower()
    if "," in number:
        if not _THOUSANDS.match(number):
            return None, None
        number = number.replace(",", "")
    value = Decimal(number)
    return (value, unit) if value < _MAX_NUMERIC else (None, None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if abs(value) < _MAX_NUMERIC * 10 else None


def product_row(product: dict[str, Any], position: int) -> dict[str, Any]:
    flags = [str(f) for f in product.get("extraction_flags") or []]
    confidence = _number(product.get("confidence"))
    if confidence is not None and not 0 <= confidence <= 1:
        confidence = None
        flags.append("confidence_out_of_range")
    weight, weight_unit = parse_weight(product.get("weight"))
    return {
        "position": position,
        "product_name": str(product.get("product_name") or "").strip(),
        "product_description": product.get("product_description"),
        "variant": product.get("variant_label"),
        "price_amount": _number(product.get("price_amount")),
        "price_currency": (product.get("price_currency") or "").strip() or "INR",
        "tax_note": product.get("tax_note"),
        "weight": weight,
        "weight_unit": weight_unit,
        "confidence": confidence,
        "extraction_flags": flags,
        "category": (product.get("category") or "").strip() or None,
        "subcategory": (product.get("subcategory") or "").strip() or None,
    }
