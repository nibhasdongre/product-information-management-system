"""Batch submission lifecycle, with recovery after a crash at any step.

States of a batch_submissions row until it is finished:
    prepared      pages are reserved; the create call has not been attempted
    submitting    the create call was started; its outcome is not yet known
    unknown       the create call failed in a way that does not say whether a batch exists
    needs_review  recovery cannot establish the correct batch; a person must decide
    submitted     the batch id is recorded (then: completed once collected, or released)

Rules the code enforces:
  - the submission and its page reservations are committed together before any create call
  - a page with a reserved or submitted status is never sent again
  - the batch id, the submitted state and the pages' submitted status are committed together
  - a candidate batch is adopted only after its results carry this submission's custom IDs
  - an unresolved submission is never dropped from tracking; it is escalated instead
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
from pipeline.pdf import catalog_path, open_catalog, render_png
from pipeline.providers.base import BatchRejected, BatchSummary, Provider, ProviderAuthError
from pipeline.repo import PageReservation, Repo

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
    catalog_id: int
    catalog: str
    page: int
    model_png: bytes
    model_dpi: int
    render_s: float

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


def custom_id(submission_id: str, extraction_id: int) -> str:
    """Unique per request and parseable back to its page row: '<12 hex of submission>-<extraction_id>'."""
    return f"{submission_id.replace('-', '')[:12]}-{extraction_id}"


def submit(*, provider: Provider, model: ModelSpec, repo: Repo, settings: Settings,
           catalog_dir: Path, catalogs: list[str], pages: list[int] | None) -> SubmitReport:
    unresolved = repo.unresolved()
    if unresolved:
        raise UnresolvedSubmissions([sub["submission_id"] for sub in unresolved])

    report = SubmitReport()
    chunk: list[_Item] = []
    chunk_bytes = 0
    for catalog in catalogs:
        catalog_id = repo.ensure_catalog(catalog, catalog_path(catalog_dir, catalog))
        statuses = repo.page_statuses(catalog_id)
        with open_catalog(catalog_dir, catalog) as doc:
            for page in pages or range(1, doc.page_count + 1):
                if statuses.get(page) in ("ok", "reserved", "submitted"):
                    report.skipped += 1
                    continue
                started = time.monotonic()
                try:
                    png = render_png(doc, page, settings.render_dpi)
                    model_png, model_dpi = model_image(doc, page, png, settings.render_dpi)
                except Exception as exc:
                    logger.exception("%s p%d could not be rendered", catalog, page)
                    repo.record_render_failure(catalog_id, page, settings.render_dpi,
                                               f"{type(exc).__name__}: {exc}", time.monotonic() - started)
                    report.failed += 1
                    continue

                item = _Item(catalog_id, catalog, page, model_png, model_dpi, time.monotonic() - started)
                full = (len(chunk) >= settings.batch_max_requests
                        or chunk_bytes + item.estimated_bytes() > settings.batch_max_bytes)
                if chunk and full:
                    report.batch_ids.append(_submit_chunk(chunk, provider, repo, settings.render_dpi))
                    chunk, chunk_bytes = [], 0
                chunk.append(item)
                chunk_bytes += item.estimated_bytes()
                report.submitted += 1

    if chunk:
        report.batch_ids.append(_submit_chunk(chunk, provider, repo, settings.render_dpi))
    return report


def _submit_chunk(chunk: list[_Item], provider: Provider, repo: Repo, dpi: int) -> str:
    submission_id = str(uuid.uuid4())
    reservations = [PageReservation(i.catalog_id, i.page, dpi, i.model_dpi, i.render_s) for i in chunk]
    extraction_ids = repo.create_submission(submission_id, now_utc(), reservations)
    params = [{"custom_id": custom_id(submission_id, extraction_ids[(i.catalog_id, i.page)]),
               "params": provider.build_request(i.model_png)} for i in chunk]
    repo.set_state(submission_id, SUBMITTING)

    try:
        batch_id = provider.submit_batch(params)
    except (BatchRejected, ProviderAuthError):
        repo.release(submission_id)
        raise
    except Exception as exc:
        repo.set_state(submission_id, UNKNOWN, f"create call outcome unknown ({type(exc).__name__}: {exc})")
        raise SubmissionUnknown(submission_id) from exc

    repo.record_batch(submission_id, batch_id)
    logger.info("submitted batch %s with %d page(s) (submission %s)", batch_id, len(chunk), submission_id)
    return batch_id


def reconcile(provider: Provider, repo: Repo, now: datetime | None = None) -> list[str]:
    """Bring every unresolved submission to a known state. Call with the batch lock held."""
    now = now or now_utc()
    messages: list[str] = []
    for sub in repo.unresolved():
        submission_id = sub["submission_id"]
        if sub["state"] == PREPARED:
            repo.release(submission_id)
            messages.append(f"{submission_id}: released; the create call was never attempted")
        elif sub["state"] == NEEDS_REVIEW:
            messages.append(f"{submission_id}: NEEDS REVIEW - {sub['review_reason']}")
        else:
            messages.append(_resolve_unknown(provider, repo, sub, now))
    return messages


def _resolve_unknown(provider: Provider, repo: Repo, sub: dict[str, Any], now: datetime) -> str:
    submission_id = sub["submission_id"]
    expected = _expected_ids(repo, submission_id)
    age = now - sub["created_at"]

    if sub["candidate_batch_id"] is None:
        due = sub["next_check_at"]
        if due and now < due:
            return f"{submission_id}: unresolved; next batch search after {due.isoformat(timespec='seconds')}"
        matches = _find_candidates(provider, repo, sub)
        if len(matches) > 1:
            ids = ", ".join(m.batch_id for m in matches)
            repo.set_state(submission_id, NEEDS_REVIEW, f"{len(matches)} batches match this submission: {ids}")
            return f"{submission_id}: NEEDS REVIEW - {len(matches)} candidate batches: {ids}"
        if not matches:
            if age >= WARN_AFTER:
                reason = f"no batch found after {age}; check the Anthropic console"
                repo.set_state(submission_id, NEEDS_REVIEW, reason)
                return f"{submission_id}: NEEDS REVIEW - {reason}"
            _schedule_search(repo, sub, now)
            return f"{submission_id}: unresolved; no batch found yet (age {age}); searching again later"
        repo.set_candidate(submission_id, matches[0].batch_id)
        sub["candidate_batch_id"] = matches[0].batch_id

    batch_id = sub["candidate_batch_id"]
    if provider.batch_status(batch_id).processing_status != "ended":
        warning = " WARNING: approaching the 24-hour expiry" if age >= WARN_AFTER else ""
        return f"{submission_id}: candidate {batch_id} is processing; verifying once it ends.{warning}"
    if _results_match(provider, batch_id, expected):
        repo.record_batch(submission_id, batch_id)
        return f"{submission_id}: verified by custom IDs and recorded batch {batch_id}"
    reason = f"candidate {batch_id} results do not match this submission"
    repo.set_state(submission_id, NEEDS_REVIEW, reason)
    return f"{submission_id}: NEEDS REVIEW - {reason}"


def _expected_ids(repo: Repo, submission_id: str) -> set[str]:
    return {custom_id(submission_id, extraction_id) for extraction_id in repo.extraction_ids(submission_id)}


def _find_candidates(provider: Provider, repo: Repo, sub: dict[str, Any]) -> list[BatchSummary]:
    since = sub["created_at"] - CLOCK_SKEW
    known = repo.known_batch_ids()
    return [b for b in provider.list_batches(since)
            if b.total == sub["count"] and b.batch_id not in known]


def _schedule_search(repo: Repo, sub: dict[str, Any], now: datetime) -> None:
    attempts = sub["check_attempts"] + 1
    delay = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (attempts - 1))
    repo.schedule_search(sub["submission_id"], attempts, now + timedelta(seconds=delay))


def _results_match(provider: Provider, batch_id: str, expected: set[str]) -> bool:
    ids = [result.custom_id for result in provider.batch_results(batch_id)]
    return len(ids) == len(expected) and set(ids) == expected


def resolve_adopt(provider: Provider, repo: Repo, submission_id: str, batch_id: str) -> str:
    """Manual decision: record batch_id as this submission's batch, if its results verify."""
    if repo.get_unresolved(submission_id) is None:
        raise KeyError(f"no unresolved submission {submission_id}")
    if batch_id in repo.known_batch_ids():
        raise ValueError(f"batch {batch_id} is already recorded for another submission")
    if provider.batch_status(batch_id).processing_status != "ended":
        raise ValueError(f"batch {batch_id} has not ended; its results are needed for verification")
    if not _results_match(provider, batch_id, _expected_ids(repo, submission_id)):
        raise ValueError(f"batch {batch_id} results do not match submission {submission_id}")
    repo.record_batch(submission_id, batch_id)
    return f"recorded batch {batch_id} for submission {submission_id}"


def resolve_release(repo: Repo, submission_id: str) -> str:
    """Manual decision: release a submission. Only correct if no batch was created for it."""
    if repo.get_unresolved(submission_id) is None:
        raise KeyError(f"no unresolved submission {submission_id}")
    repo.release(submission_id)
    return f"released submission {submission_id}; its pages are eligible again"
