"""Batch status, timing, and result collection. Submission and recovery live in submission.py.

Collect writes database rows only: status, attempt, raw model output and product rows. It needs no PDF
and no storage, so it can run on any machine that can reach the database and the Anthropic API."""
from __future__ import annotations

import logging
from typing import Any

from pipeline.config import ModelSpec
from pipeline.providers.base import BatchResult, CallMetrics, ExtractionError, Provider
from pipeline.report import wall_suffix
from pipeline.repo import Repo
from pipeline.submission import custom_id

logger = logging.getLogger(__name__)


def refresh_status(provider: Provider, repo: Repo) -> list[str]:
    lines = []
    for sub in repo.open_batches():
        status = provider.batch_status(sub["anthropic_batch_id"])
        repo.update_timing(sub["submission_id"], status.created_at, status.ended_at)
        created = status.created_at or sub["batch_created_at"]
        ended = status.ended_at or sub["batch_ended_at"]
        counts = " ".join(f"{k}={v}" for k, v in status.counts.items())
        lines.append(f"{sub['anthropic_batch_id']}  {status.processing_status:<12} {counts}  "
                     f"requests={sub['count']}{wall_suffix(created, ended)}")
    return lines


def _write_result(provider: Provider, repo: Repo, model: ModelSpec, submission_id: str,
                  extraction_id: int, page: dict[str, Any], result: BatchResult | None) -> None:
    if page["status"] != "submitted":
        return  # already collected by an earlier run

    def fail(message: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        cost = model.cost_usd(input_tokens, output_tokens, batch=True)
        repo.finish_page(extraction_id, submission_id, error=message, input_tokens=input_tokens,
                         output_tokens=output_tokens, cost_usd=cost)

    if result is None:
        return fail("batch returned no result for this request")
    if result.kind != "succeeded":
        return fail(f"batch {result.kind}: {result.detail}")

    usage = result.message.usage
    try:
        extraction = provider.parse_result(result.message, _metrics(usage))
    except ExtractionError as exc:
        return fail(str(exc), exc.metrics.input_tokens, exc.metrics.output_tokens)

    repo.finish_page(
        extraction_id, submission_id, error=None,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        cost_usd=model.cost_usd(usage.input_tokens, usage.output_tokens, batch=True),
        page_type=extraction.page_type,
        raw_output={"page_type": extraction.page_type, "products": extraction.products},
        products=extraction.products)


def _metrics(usage: Any) -> CallMetrics:
    return CallMetrics(attempts=1, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens, batch=True)


def collect(provider: Provider, repo: Repo, model: ModelSpec) -> list[str]:
    collected = []
    for sub in repo.open_batches():
        batch_id, submission_id = sub["anthropic_batch_id"], sub["submission_id"]
        status = provider.batch_status(batch_id)
        repo.update_timing(submission_id, status.created_at, status.ended_at)
        if status.processing_status != "ended":
            continue

        pages = repo.submission_pages(submission_id)
        by_custom_id = {custom_id(submission_id, extraction_id): extraction_id for extraction_id in pages}
        seen: set[int] = set()
        for result in provider.batch_results(batch_id):
            extraction_id = by_custom_id.get(result.custom_id)
            if extraction_id is None:
                logger.warning("batch %s returned unknown custom_id %s", batch_id, result.custom_id)
                continue
            seen.add(extraction_id)
            _write_result(provider, repo, model, submission_id, extraction_id, pages[extraction_id], result)
        for extraction_id, page in pages.items():
            if extraction_id not in seen:
                _write_result(provider, repo, model, submission_id, extraction_id, page, None)

        repo.complete(submission_id)
        collected.append(batch_id)
        logger.info("collected batch %s", batch_id)
    return collected
