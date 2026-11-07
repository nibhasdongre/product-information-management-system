"""The product record, declared once. The prompt field list, the tool input
schema, and response validation are all derived from FIELDS."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

TOOL_NAME = "record_page"
PAGE_TYPES = ("product", "non_product")


@dataclass(frozen=True)
class Field:
    name: str
    json_type: str | tuple[str, ...]
    description: str
    required: bool = False
    items: dict[str, Any] | None = None

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": list(self.json_type) if isinstance(self.json_type, tuple) else self.json_type
        }
        if self.items is not None:
            schema["items"] = self.items
        return schema


FIELDS: tuple[Field, ...] = (
    Field("product_name", "string",
          "string — the product's short title/name (NOT the full description)", required=True),
    Field("product_description", "string", "string — full specs/details text"),
    Field("price_amount", ("number", "null"),
          "number or null — the printed price; null if none shown", required=True),
    Field("price_currency", "string", "string — e.g. INR"),
    Field("tax_note", "string", "string — on-pack qualifier if any, e.g. +GST / MRP / incl"),
    Field("weight", ("string", "null"), "string or null — e.g. '247 gms', '500ml'"),
    Field("variant_label", ("string", "null"), "string or null — e.g. 'Box of 16', 'Large'"),
    Field("category", ("string", "null"),
          "string or null — product-type category; null if not printed on the page"),
    Field("subcategory", ("string", "null"),
          "string or null — null if not printed on the page"),
    Field("colours", ("array", "null"), "array of strings or null", items={"type": "string"}),
    Field("eco_friendly", ("boolean", "null"), "boolean or null"),
    Field("confidence", "number", "number 0-1"),
    Field("extraction_flags", "array", "array of strings — e.g. price_not_found",
          items={"type": "string"}),
)

PRODUCT_FIELDS: dict[str, str] = {f.name: f.description for f in FIELDS}


def _product_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {f.name: f.json_schema() for f in FIELDS},
        "required": [f.name for f in FIELDS if f.required],
        "additionalProperties": False,
    }


JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "page_type": {"type": "string", "enum": list(PAGE_TYPES)},
        "products": {"type": "array", "items": _product_schema()},
    },
    "required": ["page_type", "products"],
    "additionalProperties": False,
}

TOOL_DEFINITION: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": "Record the page type and every distinct product printed on this catalog page.",
    "input_schema": JSON_SCHEMA,
}

_validator = Draft202012Validator(JSON_SCHEMA)


def validate_page(data: Any) -> list[str]:
    """Return human-readable schema violations; empty list means valid."""
    return [err.message for err in _validator.iter_errors(data)]
