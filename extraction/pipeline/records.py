"""Page record format, shared by the synchronous and batch paths. A page file
always has the same shape, whichever path produced it."""
from __future__ import annotations

from typing import Any

from pipeline.config import ModelSpec
from pipeline.images import image_plan, page_image_name
from pipeline.providers.base import CallMetrics, PageExtraction


def metrics_block(metrics: CallMetrics, render_s: float, model: ModelSpec) -> dict[str, Any]:
    return {
        "mode": "batch" if metrics.batch else "sync",
        "attempts": metrics.attempts,
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
        "cost_usd": round(model.cost_usd(metrics.input_tokens, metrics.output_tokens, batch=metrics.batch), 6),
        "render_s": round(render_s, 3),
        "api_s": round(metrics.api_s, 3),
        "total_s": round(render_s + metrics.total_s, 3),
    }


def history_of(prior: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Attempts recorded by earlier runs, so a retried page keeps the cost of its failures."""
    if prior is None:
        return []
    past = list(prior.get("history", []))
    if "metrics" in prior:
        past.append({"status": prior.get("status"), "error": prior.get("error"),
                     "metrics": prior["metrics"]})
    return past


def success_record(catalog: str, page: int, dpi: int, model_dpi: int, model: ModelSpec,
                   extraction: PageExtraction, render_s: float,
                   history: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
    plan = image_plan(extraction.products)
    image_file = page_image_name(catalog, page) if plan.needs_image else None
    record = {
        "status": "ok",
        "catalog": catalog,
        "page": page,
        "model": model.name,
        "dpi": dpi,
        "model_dpi": model_dpi,
        "page_type": extraction.page_type,
        "image": {
            "action": plan.action,
            "file": image_file,
            "shared": plan.shared,
            "distinct_products": list(plan.distinct_names),
        },
        "products": [{**product, "image_file": image_file} for product in extraction.products],
        "metrics": metrics_block(extraction.metrics, render_s, model),
        "history": history,
    }
    return record, image_file


def error_record(catalog: str, page: int, model: ModelSpec, message: str,
                 metrics: CallMetrics, render_s: float,
                 history: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "error",
        "catalog": catalog,
        "page": page,
        "model": model.name,
        "error": message,
        "metrics": metrics_block(metrics, render_s, model),
        "history": history,
    }
