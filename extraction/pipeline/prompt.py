"""Extraction prompt. Field list is generated from schema.FIELDS; rules are the
tuned, evaluated text. Any change here needs re-evaluation against ground truth."""
from __future__ import annotations

from pipeline.schema import FIELDS, TOOL_NAME

_HEADER = (
    "You are extracting products from ONE page of a vendor gift catalog for a "
    "product masterdata system. Look at the page image and return every "
    "distinct product on it.\n\n"
    "Extract ONLY what is printed on the page. Focus on getting these right:\n"
    "  product_name, product_description, price_amount, price_currency, "
    "tax_note, weight, variant_label, colours — and splitting products correctly.\n\n"
    "Rules:\n"
)

_RULES = (
    "- Each size/pack VARIANT is its own product record (e.g. 'Box of 16' and "
    "'Box of 9' are two records that share the same product_name).\n"
    "- On a grid of different products, return one record per product.\n"
    "- The SAME product shown more than once on a page — multiple photos or "
    "angles, front-and-back, or a repeated hero shot — is ONE record. Do NOT "
    "emit duplicate records for one product (same name with the same "
    "price/variant). Distinct size/pack variants are the ONLY reason to repeat "
    "a product_name.\n"
    "- A gift box / hamper / combo / kit is ONE product, even when the page "
    "lists what's inside it. Put the contained items in product_description; do "
    "NOT emit those contents as separate products. (This is different from a "
    "grid of individually-sold products, which stays one record each.)\n"
    "- product_name is the short title; product_description holds the full "
    "specs/details — they are DIFFERENT fields.\n"
    "- Return an item as a product ONLY if a price is printed for it on the page. "
    "Do NOT return an item that has no printed price. This includes section-opening "
    "pages that show or name an item without a price, index / overview / contents "
    "pages that list items without prices, and covers. NEVER invent a price.\n"
    "- If a page shows several items and only some of them have a printed price, "
    "return only the ones that do.\n"
    "- If no item on the page has a printed price (a cover, section divider, "
    "index, overview or pure marketing page), return an empty products list and "
    "page_type='non_product'. A reviewer adds any real product that was left out, "
    "so when in doubt about a missing price, leave the item out.\n"
    "- category / subcategory / eco_friendly are OPTIONAL best-effort hints; "
    "they often are NOT on the page. Leave them null rather than guessing — "
    "taxonomy and attributes are assigned later by a human/classifier, not scored here.\n"
    "- Do not guess; leave a field null and flag it instead.\n\n"
)

PROMPT = (
    _HEADER
    + _RULES
    + f"When done, call the {TOOL_NAME} tool once with page_type and products.\n"
    + "Each product object has these fields:\n"
    + "\n".join(f"  - {f.name}: {f.description}" for f in FIELDS)
)
