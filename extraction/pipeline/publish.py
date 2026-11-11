"""Cut product images from catalogue PDFs for the storefront.

A product is published once both review levels of its catalogue are done. Its crop (a rectangle as
fractions of the page, or the whole page when unset) is cut from the PDF and saved in two sizes as WebP
in the public bucket:

    products/<hash>/card.webp      for product grids
    products/<hash>/detail.webp    for the product page

`<hash>` comes from the PDF's hash, the page and the crop, so the same crop of the same page is the same
name, a different crop is a new name, and a published file never changes (it can be cached forever).
`product.cropped_path` holds `products/<hash>`. A reviewer who changes a crop clears `cropped_path`, and
the next run cuts the new one; old files are left in the bucket. Every step can be repeated safely, so a
run that stops halfway is finished by the next one."""
from __future__ import annotations

import hashlib
import io
import json
import logging
from dataclasses import dataclass, field

import pymupdf
from PIL import Image

from pipeline.repo import Repo
from pipeline.storage import Storage, StorageError

logger = logging.getLogger(__name__)

SPEC = "v1"            # bump to cut every image again (new sizes or quality)
CARD_PX = 480          # longest side of the card image
DETAIL_PX = 1600       # longest side of the detail image
MAX_SCALE = 4.0        # never render above 4x the PDF's size (288 dpi); more detail would not exist
WEBP_QUALITY = 80
CACHE_FOREVER = "public, max-age=31536000, immutable"       # a published name never changes meaning


@dataclass
class PublishReport:
    published: int = 0
    changed_meanwhile: int = 0
    failed: list[tuple[int, str]] = field(default_factory=list)       # (product_id, reason)


def image_key(pdf_sha256: str, page: int, crop: dict | None) -> str:
    material = json.dumps([SPEC, pdf_sha256, page, crop], sort_keys=True, separators=(",", ":"))
    return "products/" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _webp(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "WEBP", quality=WEBP_QUALITY, method=4)
    return out.getvalue()


def cut(doc: pymupdf.Document, page_no: int, crop: dict | None) -> tuple[bytes, bytes]:
    """Return (card, detail) WebP bytes for the crop of a page. crop is fractions of the page as it is
    displayed (what the review screen shows); None means the whole page."""
    page = doc[page_no - 1]
    rect = page.rect
    if crop:
        clip = pymupdf.Rect(rect.x0 + crop["x"] * rect.width, rect.y0 + crop["y"] * rect.height,
                            rect.x0 + (crop["x"] + crop["w"]) * rect.width,
                            rect.y0 + (crop["y"] + crop["h"]) * rect.height) & rect
    else:
        clip = rect
    if clip.is_empty:
        raise ValueError("the crop lies outside the page")
    scale = min(DETAIL_PX / max(clip.width, clip.height), MAX_SCALE)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
    detail = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    card = detail.copy()
    card.thumbnail((CARD_PX, CARD_PX), Image.Resampling.LANCZOS)         # shrinks only, never enlarges
    return _webp(card), _webp(detail)


def publish(repo: Repo, pdfs: Storage, images: Storage) -> PublishReport:
    """Cut and upload every pending product image, one catalogue at a time (its PDF is fetched once)."""
    report = PublishReport()
    pending = repo.products_to_publish()
    by_catalog: dict[int, list[dict]] = {}
    for row in pending:
        by_catalog.setdefault(row["catalog_id"], []).append(row)
    uploaded: set[str] = set()
    for catalog_id, rows in by_catalog.items():
        try:
            doc = pymupdf.open(stream=pdfs.get(rows[0]["storage_path"]), filetype="pdf")
        except (StorageError, RuntimeError, ValueError) as exc:
            logger.error("catalogue %s: cannot open its PDF: %s", catalog_id, exc)
            report.failed += [(r["product_id"], f"PDF unavailable: {exc}") for r in rows]
            continue
        with doc:
            for row in rows:
                key = image_key(row["file_sha256"], row["page"], row["crop"])
                try:
                    if key not in uploaded:
                        card, detail = cut(doc, row["page"], row["crop"])
                        images.put(f"{key}/card.webp", card, "image/webp", CACHE_FOREVER)
                        images.put(f"{key}/detail.webp", detail, "image/webp", CACHE_FOREVER)
                        uploaded.add(key)
                except (StorageError, RuntimeError, ValueError) as exc:
                    logger.error("product %s: %s", row["product_id"], exc)
                    report.failed.append((row["product_id"], str(exc)))
                    continue
                if repo.set_cropped_path(row["product_id"], row["crop"], key):
                    report.published += 1
                else:
                    report.changed_meanwhile += 1
    return report
