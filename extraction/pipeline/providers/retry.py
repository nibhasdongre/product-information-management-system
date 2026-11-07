"""Exponential-backoff retry. Providers supply is_retryable, so the policy knows
nothing about any SDK's exception types."""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


class RetryFailed(Exception):
    def __init__(self, attempts: int, cause: BaseException):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.attempts = attempts
        self.cause = cause


class RetryPolicy:
    def __init__(self, tries: int, base_s: float, cap_s: float,
                 is_retryable: Callable[[BaseException], bool],
                 delay_hint: Callable[[BaseException], float | None] | None = None):
        self._tries = tries
        self._base_s = base_s
        self._cap_s = cap_s
        self._is_retryable = is_retryable
        self._delay_hint = delay_hint

    def call(self, fn: Callable[[], T], label: str) -> tuple[T, int]:
        """Return (result, attempts used). Raises RetryFailed when giving up."""
        for attempt in range(1, self._tries + 1):
            try:
                return fn(), attempt
            except Exception as exc:
                if attempt == self._tries or not self._is_retryable(exc):
                    raise RetryFailed(attempt, exc) from exc
                hinted = self._delay_hint(exc) if self._delay_hint else None
                if hinted is not None:
                    delay = hinted
                else:
                    delay = min(self._cap_s, self._base_s * 2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.warning("%s: %s on attempt %d/%d, retrying in %.1fs",
                               label, type(exc).__name__, attempt, self._tries, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")
