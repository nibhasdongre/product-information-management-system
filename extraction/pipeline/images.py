"""Image planning for Option A: one whole-page image per page, referenced by
every product on that page. Non-product pages produce no image."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


def slug(text: str, maxlen: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", str(text or "product")).strip("_")
    return cleaned[:maxlen] or "product"


@dataclass(frozen=True)
class ImagePlan:
    action: str                       # "skip" | "whole_page"
    distinct_names: tuple[str, ...]

    @property
    def needs_image(self) -> bool:
        return self.action != "skip"

    @property
    def shared(self) -> bool:
        return len(self.distinct_names) > 1


def image_plan(products: list[dict[str, Any]]) -> ImagePlan:
    if not products:
        return ImagePlan("skip", ())
    names = [p.get("product_name") for p in products if p.get("product_name")]
    return ImagePlan("whole_page", tuple(dict.fromkeys(names)))


def page_image_name(catalog: str, page: int) -> str:
    return f"{slug(catalog, 60)}_p{page:03d}.png"
