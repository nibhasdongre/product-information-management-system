"""Batch status, timing, and result collection. Submission and recovery live in submission.py."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pipeline.config import ModelSpec
from pipeline.providers.base import BatchResult, BatchStatus, CallMetrics, ExtractionError, Provider
from pipeline.records import error_record, success_record
from pipeline.report import batch_wall_time
from pipeline.store import OutputStore

logger = logging.getLogger(__name__)


def _stamp(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="seconds") if moment else None


def _apply_timing(manifest: dict[str, Any], status: BatchStatus) -> bool:
    """Copy Anthropic's created and ended times into the manifest. Returns True if anything changed."""
    changed = False
    for key, value in (("batch_created_at", _stamp(status.created_at)),
                       ("batch_ended_at", _stamp(status.ended_at))):
        if value is not None and manifest.get(key) != value:
            manifest[key] = value
            changed = True
        manifest.setdefault(key, None)
    return changed


def _wall_suffix(manifest: dict[str, Any]) -> str:
    wall = batch_wall_time(manifest)
    return f"  wall={wall}" if wall is not None else ""


def refresh_status(provider: Provider, store: OutputStore) -> list[str]:
    lines = []
    for manifest in store.list_manifests():
        if manifest["collected"]:
            continue
        status = provider.batch_status(manifest["batch_id"])
        manifest["processing_status"] = status.processing_status
        manifest["counts"] = status.counts
        _apply_timing(manifest, status)
        store.save_manifest(manifest)
        counts = " ".join(f"{k}={v}" for k, v in status.counts.items())
        lines.append(f"{manifest['batch_id']}  {status.processing_status:<12} {counts}  "
                     f"requests={len(manifest['requests'])}{_wall_suffix(manifest)}")
    return lines


def _write_result(provider: Provider, store: OutputStore, model: ModelSpec, submission_id: str,
                  custom_id: str, meta: dict[str, Any], result: BatchResult | None) -> None:
    catalog, page, history = meta["catalog"], meta["page"], meta["history"]
    if store.is_done(catalog, page):
        return  # already collected by an earlier run
    render_s = meta["render_s"]

    if result is None:
        record = error_record(catalog, page, model, "batch returned no result for this request",
                              CallMetrics(batch=True), render_s, history)
    elif result.kind != "succeeded":
        record = error_record(catalog, page, model, f"batch {result.kind}: {result.detail}",
                              CallMetrics(batch=True), render_s, history)
    else:
        usage = result.message.usage
        metrics = CallMetrics(attempts=1, input_tokens=usage.input_tokens,
                              output_tokens=usage.output_tokens, batch=True)
        try:
            extraction = provider.parse_result(result.message, metrics)
        except ExtractionError as exc:
            record = error_record(catalog, page, model, str(exc), exc.metrics, render_s, history)
        else:
            png = store.load_staged(submission_id, custom_id)
            if png is None:
                record = error_record(catalog, page, model, "staged page image missing",
                                      metrics, render_s, history)
            else:
                record, image_file = success_record(catalog, page, meta["dpi"], meta["model_dpi"],
                                                    model, extraction, render_s, history)
                if image_file:
                    store.save_image(catalog, image_file, png)

    store.save_page(catalog, page, record)


def collect(provider: Provider, store: OutputStore, model: ModelSpec) -> list[str]:
    collected = []
    for manifest in store.list_manifests():
        if manifest["collected"]:
            continue
        batch_id = manifest["batch_id"]
        submission_id = manifest.get("submission_id")
        if submission_id is None:
            logger.warning("batch %s was recorded before submissions were tracked; not collected", batch_id)
            continue
        status = provider.batch_status(batch_id)
        if _apply_timing(manifest, status):
            store.save_manifest(manifest)
        if status.processing_status != "ended":
            continue

        seen: set[str] = set()
        for result in provider.batch_results(batch_id):
            seen.add(result.custom_id)
            meta = manifest["requests"].get(result.custom_id)
            if meta is None:
                logger.warning("batch %s returned unknown custom_id %s", batch_id, result.custom_id)
                continue
            _write_result(provider, store, model, submission_id, result.custom_id, meta, result)
        for custom_id, meta in manifest["requests"].items():
            if custom_id not in seen:
                _write_result(provider, store, model, submission_id, custom_id, meta, None)

        store.drop_staged(submission_id)
        manifest["processing_status"] = "ended"
        manifest["collected"] = True
        store.save_manifest(manifest)
        collected.append(batch_id)
        logger.info("collected batch %s", batch_id)
    return collected
