from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CallMetrics:
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    api_s: float = 0.0    # duration of the successful API call
    total_s: float = 0.0  # all attempts plus backoff waits
    batch: bool = False   # True when the answer came from the Batch API (discounted, no per-call latency)


class ProviderAuthError(RuntimeError):
    """The API rejected our credentials. Every page would fail the same way, so the run stops."""


class BatchRejected(RuntimeError):
    """The API refused a batch create request. The request was not accepted, so no batch exists."""


class ExtractionError(RuntimeError):
    """The page could not be extracted. Carries the metrics incurred so far,
    because an unusable answer is still billed."""

    def __init__(self, message: str, metrics: CallMetrics):
        super().__init__(message)
        self.metrics = metrics


@dataclass(frozen=True)
class PageExtraction:
    page_type: str
    products: list[dict[str, Any]]
    metrics: CallMetrics


@dataclass(frozen=True)
class BatchStatus:
    processing_status: str   # "in_progress" | "canceling" | "ended"
    counts: dict[str, int]
    created_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True)
class BatchSummary:
    batch_id: str
    created_at: datetime
    total: int
    processing_status: str


@dataclass(frozen=True)
class BatchResult:
    custom_id: str
    kind: str                # "succeeded" | "errored" | "canceled" | "expired"
    message: Any = None      # provider response object when kind == "succeeded"
    detail: str = ""


class Provider(ABC):
    @abstractmethod
    def extract_page(self, png: bytes) -> PageExtraction:
        """Raise ExtractionError for unusable output; transport errors are retried internally."""

    @abstractmethod
    def build_request(self, png: bytes) -> dict[str, Any]:
        """Parameters for one page request, shared by the synchronous and batch paths."""

    @abstractmethod
    def parse_result(self, message: Any, metrics: CallMetrics) -> PageExtraction:
        """Validate a model response. Raise ExtractionError (carrying metrics) if unusable."""

    @abstractmethod
    def submit_batch(self, requests: list[dict[str, Any]]) -> str:
        """Create a batch from (custom_id, params) requests and return its id."""

    @abstractmethod
    def batch_status(self, batch_id: str) -> BatchStatus:
        ...

    @abstractmethod
    def batch_results(self, batch_id: str) -> Iterable[BatchResult]:
        ...

    @abstractmethod
    def list_batches(self, since: datetime) -> list[BatchSummary]:
        """Batches created at or after `since`, newest first."""
