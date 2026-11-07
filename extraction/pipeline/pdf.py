"""PDF discovery and page rendering (PyMuPDF). A catalog key is a PDF file stem."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pymupdf as fitz


def discover(catalog_dir: Path) -> list[str]:
    return sorted(p.stem for p in catalog_dir.glob("*.pdf"))


def catalog_path(catalog_dir: Path, key: str) -> Path:
    path = catalog_dir / f"{key}.pdf"
    if not path.is_file():
        raise FileNotFoundError(f"No PDF for catalog '{key}' in {catalog_dir}")
    return path


@contextmanager
def open_catalog(catalog_dir: Path, key: str) -> Iterator[fitz.Document]:
    with fitz.open(str(catalog_path(catalog_dir, key))) as doc:
        yield doc


def render_png(doc: fitz.Document, page_no: int, dpi: int) -> bytes:
    """Render one page (1-indexed) to PNG bytes."""
    return doc[page_no - 1].get_pixmap(dpi=dpi).tobytes("png")
