from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from pipeline.config import ModelSpec, RetrySettings
from pipeline.prompt import PROMPT
from pipeline.providers.base import (
    BatchRejected,
    BatchResult,
    BatchStatus,
    BatchSummary,
    CallMetrics,
    ExtractionError,
    PageExtraction,
    Provider,
    ProviderAuthError,
)
from pipeline.providers.retry import RetryFailed, RetryPolicy
from pipeline.schema import TOOL_DEFINITION, TOOL_NAME, validate_page

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_BATCH_COUNT_FIELDS = ("processing", "succeeded", "errored", "canceled", "expired")
# A refused create request was never accepted, so no batch exists for it.
_DEFINITE_REJECTION_STATUS = frozenset({400, 413})
# Longer Retry-After values are not waited out: a create call that gives up is simply released.
_MAX_RETRY_AFTER_S = 300.0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return getattr(exc, "status_code", None) in _RETRYABLE_STATUS


def _retry_after_header(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        return max(float(response.headers.get("retry-after")), 0.0)
    except (TypeError, ValueError):
        return None


def _bounded_retry_after(exc: BaseException) -> float | None:
    seconds = _retry_after_header(exc)
    return None if seconds is None else min(seconds, _MAX_RETRY_AFTER_S)


def _is_received_rate_limit(exc: BaseException) -> bool:
    """True only for a 429 that Anthropic itself sent: it carries a request-id header and
    a rate_limit_error body. A proxy's 429, or an error that merely mentions 429, does not."""
    if not isinstance(exc, anthropic.APIStatusError) or exc.status_code != 429:
        return False
    response = getattr(exc, "response", None)
    if response is None or "request-id" not in response.headers:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("type") == "rate_limit_error"


def _create_wait(exc: BaseException, attempt: int, settings: RetrySettings) -> float | None:
    """Seconds to wait before retrying a received 429 on the create call, or None to give up."""
    hinted = _retry_after_header(exc)
    if hinted is not None:
        return None if hinted >= _MAX_RETRY_AFTER_S else hinted
    return min(settings.cap_s, settings.base_s * 2 ** (attempt - 1)) + random.uniform(0, 1)


def _counts(request_counts: Any) -> dict[str, int]:
    return {name: int(getattr(request_counts, name, 0)) for name in _BATCH_COUNT_FIELDS}


def _as_datetime(value: Any) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _decode_string_arrays(data: dict) -> dict:
    """The model sometimes returns the products array as a JSON string, occasionally with
    stray characters after it. Decode the leading array and ignore the rest: the content is
    the answer, and the schema check still runs on the result."""
    value = data.get("products")
    if isinstance(value, str):
        try:
            data["products"], _ = json.JSONDecoder().raw_decode(value.lstrip())
            logger.warning("products returned as a JSON string; decoded")
        except json.JSONDecodeError:
            pass
    return data


class ClaudeProvider(Provider):
    def __init__(self, model: ModelSpec, retry: RetrySettings,
                 client: anthropic.Anthropic | None = None):
        if client is None and not (os.environ.get("ANTHROPIC_API_KEY")
                                   or (Path.home() / ".config" / "anthropic").exists()):
            raise RuntimeError("No Anthropic credentials: set ANTHROPIC_API_KEY in pim/.env "
                               "or run `ant auth login`.")
        self._model = model
        self._settings = retry
        self._retry = RetryPolicy(retry.tries, retry.base_s, retry.cap_s, _is_retryable,
                                  delay_hint=_bounded_retry_after)
        # The SDK's own retries are off so backoff happens in exactly one place.
        self._client = client or anthropic.Anthropic(max_retries=0)

    def build_request(self, png: bytes) -> dict[str, Any]:
        return {
            "model": self._model.name,
            "max_tokens": self._model.max_output_tokens,
            "tools": [TOOL_DEFINITION],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(png).decode("ascii"),
                }},
                {"type": "text", "text": PROMPT},
            ]}],
        }

    def extract_page(self, png: bytes) -> PageExtraction:
        request = self.build_request(png)
        started = time.monotonic()
        try:
            (message, api_s), attempts = self._retry.call(lambda: self._create(request), label="claude")
        except RetryFailed as exc:
            if isinstance(exc.cause, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                raise ProviderAuthError(f"Anthropic rejected the API key: {exc}") from exc.cause
            raise ExtractionError(
                f"API call failed after {exc.attempts} attempt(s): {exc}",
                CallMetrics(attempts=exc.attempts, total_s=time.monotonic() - started),
            ) from exc.cause

        metrics = CallMetrics(
            attempts=attempts,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            api_s=api_s,
            total_s=time.monotonic() - started,
        )
        return self.parse_result(message, metrics)

    def parse_result(self, message: Any, metrics: CallMetrics) -> PageExtraction:
        if message.stop_reason == "max_tokens":
            raise ExtractionError(
                f"output truncated at max_tokens={self._model.max_output_tokens}", metrics)
        block = next((b for b in message.content
                      if b.type == "tool_use" and b.name == TOOL_NAME), None)
        if block is None:
            raise ExtractionError(f"model did not call {TOOL_NAME}", metrics)
        data = _decode_string_arrays(dict(block.input))
        problems = validate_page(data)
        if problems:
            raise ExtractionError("schema violation: " + "; ".join(problems[:3]), metrics)
        return PageExtraction(page_type=data["page_type"], products=data["products"], metrics=metrics)

    def submit_batch(self, requests: list[dict[str, Any]]) -> str:
        # Only a received 429 is retried, because it means the batch was not created. Any other
        # failure is never retried: a timeout may still have created a batch, and a retry would
        # create a second one. Failures other than a refusal are left to the caller.
        for attempt in range(1, self._settings.tries + 1):
            try:
                batch = self._client.messages.batches.create(requests=requests)
                return batch.id
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                raise ProviderAuthError(f"Anthropic rejected the API key: {exc}") from exc
            except anthropic.APIStatusError as exc:
                if exc.status_code in _DEFINITE_REJECTION_STATUS:
                    raise BatchRejected(f"batch request refused: {exc}") from exc
                if not _is_received_rate_limit(exc):
                    raise
                wait = _create_wait(exc, attempt, self._settings)
                if wait is None or attempt == self._settings.tries:
                    raise BatchRejected(
                        f"create call rate-limited by Anthropic on attempt {attempt}; no batch was created"
                    ) from exc
                logger.warning("create call rate-limited (attempt %d/%d); waiting %.1fs",
                               attempt, self._settings.tries, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def batch_status(self, batch_id: str) -> BatchStatus:
        batch = self._call(lambda: self._client.messages.batches.retrieve(batch_id),
                           label="claude-batch-status")
        return BatchStatus(
            processing_status=batch.processing_status,
            counts=_counts(batch.request_counts),
            created_at=_as_datetime(batch.created_at) if batch.created_at else None,
            ended_at=_as_datetime(batch.ended_at) if batch.ended_at else None,
        )

    def list_batches(self, since: datetime) -> list[BatchSummary]:
        def fetch() -> list[BatchSummary]:
            found = []
            for batch in self._client.messages.batches.list(limit=100):
                created = _as_datetime(batch.created_at)
                if created < since:
                    break
                found.append(BatchSummary(batch.id, created, sum(_counts(batch.request_counts).values()),
                                          batch.processing_status))
            return found
        return self._call(fetch, label="claude-batch-list")

    def _call(self, fn, label: str):
        try:
            value, _ = self._retry.call(fn, label=label)
        except RetryFailed as exc:
            if isinstance(exc.cause, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                raise ProviderAuthError(f"Anthropic rejected the API key: {exc}") from exc.cause
            raise exc.cause
        return value

    def batch_results(self, batch_id: str) -> Iterable[BatchResult]:
        for entry in self._client.messages.batches.results(batch_id):
            result = entry.result
            if result.type == "succeeded":
                yield BatchResult(entry.custom_id, "succeeded", message=result.message)
            else:
                yield BatchResult(entry.custom_id, result.type, detail=str(getattr(result, "error", "")))

    def _create(self, request: dict) -> tuple[anthropic.types.Message, float]:
        started = time.monotonic()
        message = self._client.messages.create(**request)
        return message, time.monotonic() - started
