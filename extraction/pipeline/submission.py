"""Batch submission lifecycle, with recovery after a crash at any step.

Intent states, kept on disk until a submission is finished:
    prepared      images and intent are durable; the create call has not been attempted
    submitting    the create call was started; its outcome is not yet known
    unknown       the create call failed in a way that does not say whether a batch exists
    needs_review  recovery cannot establish the correct batch; a person must decide

Rules the code enforces:
  - an intent is written before any create call, and a page is reserved before its intent is final
  - a page with a reserved or submitted status is never sent again
  - a manifest is written durably before its intent is removed
  - a candidate batch is adopted only after its results carry this submission's custom IDs
  - an unresolved submission is never removed from tracking; it is escalated instead
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.config import ModelSpec, Settings
from pipeline.model_image import model_image
from pipeline.pdf import open_catalog, render_png
from pipeline.providers.base import BatchRejected, BatchSummary, CallMetrics, Provider, ProviderAuthError
from pipeline.records import error_record, history_of
from pipeline.store import RELEASED, RESERVED, SUBMITTED, OutputStore

logger = logging.getLogger(__name__)

PREPARED = "prepared"
SUBMITTING = "submitting"
UNKNOWN = "unknown"
NEEDS_REVIEW = "needs_review"

WARN_AFTER = timedelta(hours=20)  # batches expire 24 hours after creation
CLOCK_SKEW = timedelta(minutes=2)
_BACKOFF_BASE_S = 60
_BACKOFF_CAP_S = 900
# Prompt and tool schema are sent with every request; this budgets for them in the size cap.
_PROMPT_OVERHEAD_BYTES = 64 * 1024


class UnresolvedSubmissions(RuntimeError):
    def __init__(self, submission_ids: list[str]):
        super().__init__("unresolved submissions must be resolved first: " + ", ".join(submission_ids))
        self.submission_ids = submission_ids


class SubmissionUnknown(RuntimeError):
    """The create call's outcome is unknown. Stop; run `batch status` to reconcile."""


@dataclass
class _Item:
    catalog: str
    page: int
    png: bytes
    model_png: bytes
    model_dpi: int
    render_s: float
    history: list[dict[str, Any]]

    def estimated_bytes(self) -> int:
        return len(self.model_png) * 4 // 3 + _PROMPT_OVERHEAD_BYTES


@dataclass
class SubmitReport:
    batch_ids: list[str] = field(default_factory=list)
    submitted: int = 0
    skipped: int = 0
    failed: int = 0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _custom_id(submission_id: str, index: int) -> str:
    return f"{submission_id[:12]}-{index:05d}"


def _page_record(status: str, meta: dict[str, Any], model_name: str, submission_id: str,
                 custom_id: str, batch_id: str | None = None) -> dict[str, Any]:
    record = {
        "status": status,
        "catalog": meta["catalog"],
        "page": meta["page"],
        "model": model_name,
        "submission_id": submission_id,
        "custom_id": custom_id,
        "history": meta["history"],
    }
    if batch_id is not None:
        record["batch_id"] = batch_id
    return record


def _set_state(store: OutputStore, intent: dict[str, Any], state: str, reason: str | None = None) -> None:
    intent["state"] = state
    if reason is not None:
        intent["review_reason"] = reason
    store.save_intent(intent)


def _record_batch(store: OutputStore, intent: dict[str, Any], batch_id: str) -> None:
    store.save_manifest({
        "batch_id": batch_id,
        "model": intent["model"],
        "submission_id": intent["submission_id"],
        "submitted_at": intent["created_at"],
        "processing_status": "in_progress",
        "counts": {},
        "collected": False,
        "requests": intent["requests"],
    })
    _finalize(store, intent, batch_id)


def _finalize(store: OutputStore, intent: dict[str, Any], batch_id: str) -> None:
    """Idempotent: safe to run again after a crash part-way through."""
    submission_id = intent["submission_id"]
    for custom_id, meta in intent["requests"].items():
        store.save_page(meta["catalog"], meta["page"],
                        _page_record(SUBMITTED, meta, intent["model"], submission_id, custom_id, batch_id))
    store.delete_intent(submission_id)


def _release(store: OutputStore, intent: dict[str, Any]) -> None:
    """Only for submissions whose create call was never attempted, or was definitively refused."""
    submission_id = intent["submission_id"]
    for custom_id, meta in intent["requests"].items():
        store.save_page(meta["catalog"], meta["page"],
                        _page_record(RELEASED, meta, intent["model"], submission_id, custom_id))
    store.drop_staged(submission_id)
    store.delete_intent(submission_id)


def submit(*, provider: Provider, model: ModelSpec, store: OutputStore, settings: Settings,
           catalog_dir: Path, catalogs: list[str], pages: list[int] | None) -> SubmitReport:
    unresolved = store.list_intents()
    if unresolved:
        raise UnresolvedSubmissions([intent["submission_id"] for intent in unresolved])

    report = SubmitReport()
    chunk: list[_Item] = []
    chunk_bytes = 0
    for catalog in catalogs:
        with open_catalog(catalog_dir, catalog) as doc:
            for page in pages or range(1, doc.page_count + 1):
                if store.is_done(catalog, page) or store.is_in_flight(catalog, page):
                    report.skipped += 1
                    continue
                history = history_of(store.load_page(catalog, page))
                started = time.monotonic()
                try:
                    png = render_png(doc, page, settings.render_dpi)
                    model_png, model_dpi = model_image(doc, page, png, settings.render_dpi)
                except Exception as exc:
                    logger.exception("%s p%d could not be rendered", catalog, page)
                    store.save_page(catalog, page, error_record(
                        catalog, page, model, f"{type(exc).__name__}: {exc}",
                        CallMetrics(batch=True), time.monotonic() - started, history))
                    report.failed += 1
                    continue

                item = _Item(catalog, page, png, model_png, model_dpi, time.monotonic() - started, history)
                full = (len(chunk) >= settings.batch_max_requests
                        or chunk_bytes + item.estimated_bytes() > settings.batch_max_bytes)
                if chunk and full:
                    report.batch_ids.append(_submit_chunk(chunk, provider, model, store, settings.render_dpi))
                    chunk, chunk_bytes = [], 0
                chunk.append(item)
                chunk_bytes += item.estimated_bytes()
                report.submitted += 1

    if chunk:
        report.batch_ids.append(_submit_chunk(chunk, provider, model, store, settings.render_dpi))
    return report


def _submit_chunk(chunk: list[_Item], provider: Provider, model: ModelSpec,
                  store: OutputStore, dpi: int) -> str:
    submission_id = uuid.uuid4().hex
    params: list[dict[str, Any]] = []
    requests: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(chunk):
        custom_id = _custom_id(submission_id, index)
        params.append({"custom_id": custom_id, "params": provider.build_request(item.model_png)})
        requests[custom_id] = {
            "catalog": item.catalog,
            "page": item.page,
            "dpi": dpi,
            "model_dpi": item.model_dpi,
            "render_s": round(item.render_s, 3),
            "history": item.history,
        }
        store.stage_image(submission_id, custom_id, item.png)

    intent: dict[str, Any] = {
        "submission_id": submission_id,
        "model": model.name,
        "state": PREPARED,
        "created_at": _iso(now_utc()),
        "count": len(chunk),
        "requests": requests,
        "candidate_batch_id": None,
        "next_check_at": None,
        "check_attempts": 0,
        "review_reason": "",
    }
    store.save_intent(intent)
    for custom_id, meta in requests.items():
        store.save_page(meta["catalog"], meta["page"],
                        _page_record(RESERVED, meta, model.name, submission_id, custom_id))
    _set_state(store, intent, SUBMITTING)

    try:
        batch_id = provider.submit_batch(params)
    except (BatchRejected, ProviderAuthError):
        _release(store, intent)
        raise
    except Exception as exc:
        _set_state(store, intent, UNKNOWN, f"create call outcome unknown ({type(exc).__name__}: {exc})")
        raise SubmissionUnknown(submission_id) from exc

    _record_batch(store, intent, batch_id)
    logger.info("submitted batch %s with %d page(s) (submission %s)", batch_id, len(chunk), submission_id)
    return batch_id


def reconcile(provider: Provider, store: OutputStore, now: datetime | None = None) -> list[str]:
    """Bring every unresolved submission to a known state. Call with the submission lock held."""
    now = now or now_utc()
    messages: list[str] = []

    for submission_id in store.staged_submission_ids():
        if store.load_intent(submission_id) is None and store.manifest_for_submission(submission_id) is None:
            store.drop_staged(submission_id)
            messages.append(f"{submission_id}: removed images staged before an intent was written")

    for intent in store.list_intents():
        submission_id = intent["submission_id"]
        manifest = store.manifest_for_submission(submission_id)
        if manifest is not None:
            _finalize(store, intent, manifest["batch_id"])
            messages.append(f"{submission_id}: completed local records for batch {manifest['batch_id']}")
        elif intent["state"] == PREPARED:
            _release(store, intent)
            messages.append(f"{submission_id}: released; the create call was never attempted")
        elif intent["state"] == NEEDS_REVIEW:
            messages.append(f"{submission_id}: NEEDS REVIEW - {intent['review_reason']}")
        else:
            messages.append(_resolve_unknown(provider, store, intent, now))
    return messages


def _resolve_unknown(provider: Provider, store: OutputStore, intent: dict[str, Any], now: datetime) -> str:
    submission_id = intent["submission_id"]
    expected = set(intent["requests"])
    age = now - _parse(intent["created_at"])

    if intent["candidate_batch_id"] is None:
        due = intent["next_check_at"]
        if due and now < _parse(due):
            return f"{submission_id}: unresolved; next batch search after {due}"
        matches = _find_candidates(provider, store, intent)
        if len(matches) > 1:
            ids = ", ".join(m.batch_id for m in matches)
            _set_state(store, intent, NEEDS_REVIEW, f"{len(matches)} batches match this submission: {ids}")
            return f"{submission_id}: NEEDS REVIEW - {len(matches)} candidate batches: {ids}"
        if not matches:
            if age >= WARN_AFTER:
                reason = f"no batch found after {age}; check the Anthropic console"
                _set_state(store, intent, NEEDS_REVIEW, reason)
                return f"{submission_id}: NEEDS REVIEW - {reason}"
            _schedule_search(store, intent, now)
            return f"{submission_id}: unresolved; no batch found yet (age {age}); searching again later"
        intent["candidate_batch_id"] = matches[0].batch_id
        store.save_intent(intent)

    batch_id = intent["candidate_batch_id"]
    if provider.batch_status(batch_id).processing_status != "ended":
        warning = " WARNING: approaching the 24-hour expiry" if age >= WARN_AFTER else ""
        return f"{submission_id}: candidate {batch_id} is processing; verifying once it ends.{warning}"
    if _results_match(provider, batch_id, expected):
        _record_batch(store, intent, batch_id)
        return f"{submission_id}: verified by custom IDs and recorded batch {batch_id}"
    reason = f"candidate {batch_id} results do not match this submission"
    _set_state(store, intent, NEEDS_REVIEW, reason)
    return f"{submission_id}: NEEDS REVIEW - {reason}"


def _find_candidates(provider: Provider, store: OutputStore, intent: dict[str, Any]) -> list[BatchSummary]:
    since = _parse(intent["created_at"]) - CLOCK_SKEW
    known = store.known_batch_ids()
    return [b for b in provider.list_batches(since)
            if b.total == intent["count"] and b.batch_id not in known]


def _schedule_search(store: OutputStore, intent: dict[str, Any], now: datetime) -> None:
    attempts = intent["check_attempts"] + 1
    delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (attempts - 1))
    intent["check_attempts"] = attempts
    intent["next_check_at"] = _iso(now + timedelta(seconds=delay))
    store.save_intent(intent)


def _results_match(provider: Provider, batch_id: str, expected: set[str]) -> bool:
    ids = [result.custom_id for result in provider.batch_results(batch_id)]
    return len(ids) == len(expected) and set(ids) == expected


def resolve_adopt(provider: Provider, store: OutputStore, submission_id: str, batch_id: str) -> str:
    """Manual decision: record batch_id as this submission's batch, if its results verify."""
    intent = store.load_intent(submission_id)
    if intent is None:
        raise KeyError(f"no unresolved submission {submission_id}")
    if batch_id in store.known_batch_ids():
        raise ValueError(f"batch {batch_id} is already recorded for another submission")
    if provider.batch_status(batch_id).processing_status != "ended":
        raise ValueError(f"batch {batch_id} has not ended; its results are needed for verification")
    if not _results_match(provider, batch_id, set(intent["requests"])):
        raise ValueError(f"batch {batch_id} results do not match submission {submission_id}")
    _record_batch(store, intent, batch_id)
    return f"recorded batch {batch_id} for submission {submission_id}"


def resolve_release(store: OutputStore, submission_id: str) -> str:
    """Manual decision: release a submission. Only correct if no batch was created for it."""
    intent = store.load_intent(submission_id)
    if intent is None:
        raise KeyError(f"no unresolved submission {submission_id}")
    _release(store, intent)
    return f"released submission {submission_id}; its pages are eligible again"
