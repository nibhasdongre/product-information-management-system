"""Rate-limit rules for the batch create call. A received 429 is retried with Retry-After
when given, otherwise bounded backoff. Only a received 429 that runs out of retries is a
refusal; a proxy 429 or an error that merely mentions 429 is not."""
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from pipeline import submission
from pipeline.config import ModelSpec, RetrySettings
from pipeline.providers import claude as claude_module
from pipeline.providers.base import BatchRejected
from pipeline.providers.claude import ClaudeProvider

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages/batches")


def received_429(retry_after: str | None = None):
    headers = {"request-id": "req_test"}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
    response = httpx.Response(429, headers=headers, json=body, request=REQUEST)
    return anthropic.RateLimitError("Error code: 429", response=response, body=body)


def proxy_429():
    response = httpx.Response(429, headers={}, text="<html>Too Many Requests</html>", request=REQUEST)
    return anthropic.RateLimitError("Error code: 429", response=response, body=None)


def server_error_mentioning_429():
    response = httpx.Response(500, headers={"request-id": "req_x"},
                              json={"type": "error", "error": {"type": "api_error", "message": "429 internal"}},
                              request=REQUEST)
    return anthropic.InternalServerError("Error code: 500 - 429 internal", response=response, body=None)


class ScriptedCreate:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def create(self, requests):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(id=outcome)


def make_provider(outcomes, tries=3):
    script = ScriptedCreate(outcomes)
    client = SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(create=script.create)))
    model = ModelSpec(name="claude-sonnet-5", provider="claude", max_output_tokens=8192,
                      input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, batch_discount=0.5)
    provider = ClaudeProvider(model, RetrySettings(tries=tries, base_s=2.0, cap_s=30.0), client=client)
    return provider, script


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(claude_module.time, "sleep", recorded.append)
    return recorded


def test_received_429_with_retry_after_is_retried_and_then_accepted(sleeps):
    provider, script = make_provider([received_429("2"), received_429("2"), "msgbatch_ok"])
    assert provider.submit_batch([{"custom_id": "c"}]) == "msgbatch_ok"
    assert script.calls == 3
    assert sleeps == [2.0, 2.0]


def test_received_429_without_retry_after_uses_bounded_backoff(sleeps):
    provider, script = make_provider([received_429(), received_429(), "msgbatch_ok"])
    assert provider.submit_batch([{"custom_id": "c"}]) == "msgbatch_ok"
    assert script.calls == 3
    assert len(sleeps) == 2
    assert all(0 < wait <= 30.0 + 1.0 for wait in sleeps)


def test_received_429_that_exhausts_retries_is_a_refusal(sleeps):
    provider, script = make_provider([received_429("1")] * 3, tries=3)
    with pytest.raises(BatchRejected):
        provider.submit_batch([{"custom_id": "c"}])
    assert script.calls == 3


def test_retry_after_longer_than_the_bound_is_refused_without_waiting(sleeps):
    provider, script = make_provider([received_429("9999")])
    with pytest.raises(BatchRejected):
        provider.submit_batch([{"custom_id": "c"}])
    assert script.calls == 1
    assert sleeps == []


def test_proxy_429_is_not_a_refusal(sleeps):
    provider, script = make_provider([proxy_429()])
    with pytest.raises(anthropic.RateLimitError):
        provider.submit_batch([{"custom_id": "c"}])
    assert script.calls == 1
    assert sleeps == []


def test_error_that_only_mentions_429_is_not_a_refusal(sleeps):
    provider, script = make_provider([server_error_mentioning_429()])
    with pytest.raises(anthropic.InternalServerError):
        provider.submit_batch([{"custom_id": "c"}])
    assert script.calls == 1


def test_exhausted_429_releases_pages_in_the_submission_flow(env, sleeps):
    provider, script = make_provider([received_429("1")] * 3, tries=3)
    with pytest.raises(BatchRejected):
        submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings, storage=env.storage,
                          catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    assert env.repo.unresolved() == []
    assert [s["state"] for s in env.submissions()] == ["released"]
    assert env.submissions()[0]["anthropic_batch_id"] is None
    assert env.page_statuses() == ["released"] * 3
