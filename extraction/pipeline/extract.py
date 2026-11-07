"""Synchronous per-catalog extraction loop. Each page is rendered once; the same
render goes to the model (or a fitted copy) and to the saved page image."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import ModelSpec
from pipeline.model_image import model_image
from pipeline.pdf import open_catalog, render_png
from pipeline.providers.base import CallMetrics, ExtractionError, Provider, ProviderAuthError
from pipeline.records import error_record, history_of, success_record
from pipeline.store import OutputStore

logger = logging.getLogger(__name__)


@dataclass
class CatalogReport:
    catalog: str
    ok: int = 0
    skipped: int = 0
    failed: int = 0


def extract_catalog(
    *,
    catalog: str,
    catalog_dir: Path,
    provider: Provider,
    model: ModelSpec,
    store: OutputStore,
    dpi: int,
    pages: list[int] | None = None,
) -> CatalogReport:
    report = CatalogReport(catalog)
    with open_catalog(catalog_dir, catalog) as doc:
        for page in pages or range(1, doc.page_count + 1):
            if store.is_done(catalog, page) or store.is_in_flight(catalog, page):
                report.skipped += 1
                continue

            history = history_of(store.load_page(catalog, page))
            started = time.monotonic()
            model_dpi = dpi
            try:
                png = render_png(doc, page, dpi)
                model_png, model_dpi = model_image(doc, page, png, dpi)
                render_s = time.monotonic() - started
                extraction = provider.extract_page(model_png)
            except ProviderAuthError:
                raise
            except Exception as exc:  # page-level isolation: one bad page must not stop the run
                render_s = time.monotonic() - started
                if isinstance(exc, ExtractionError):
                    logger.warning("%s p%d failed: %s", catalog, page, exc)
                    metrics = exc.metrics
                else:
                    logger.exception("%s p%d failed", catalog, page)
                    metrics = CallMetrics()
                store.save_page(catalog, page, error_record(
                    catalog, page, model, f"{type(exc).__name__}: {exc}",
                    metrics, render_s, history))
                report.failed += 1
                continue

            record, image_file = success_record(catalog, page, dpi, model_dpi, model,
                                                extraction, render_s, history)
            if image_file:
                store.save_image(catalog, image_file, png)
            store.save_page(catalog, page, record)
            report.ok += 1
            m = record["metrics"]
            logger.info("%s p%d ok attempts=%d products=%d cost=$%.4f api=%.1fs total=%.1fs",
                        catalog, page, m["attempts"], len(record["products"]),
                        m["cost_usd"], m["api_s"], m["total_s"])
    return report
