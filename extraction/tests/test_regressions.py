"""Regression tests for two issues found in the real-API runs:
- a page selection like "1-" crashed with a traceback instead of a usage error
- a batch created five minutes before an intent was treated as its candidate
"""
import argparse
from datetime import timedelta

import pytest

from fakes import FakeProvider, make_env

from pipeline import submission
from pipeline.cli import _build_parser, _parse_pages


def test_valid_page_selections_are_expanded_and_sorted():
    assert _parse_pages("1,3-5") == [1, 3, 4, 5]
    assert _parse_pages(" 2 , 1 ") == [1, 2]


@pytest.mark.parametrize("spec", ["1-", "-3", "0", "5-2", "x", ",", ""])
def test_invalid_page_selections_are_rejected(spec):
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_pages(spec)


def test_cli_reports_bad_page_selection_as_usage_error(capsys):
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["batch", "submit", "Turtle", "--pages", "1-"])
    assert "invalid page selection" in capsys.readouterr().err


def test_batch_created_five_minutes_before_the_intent_is_not_a_candidate(tmp_path):
    env = make_env(tmp_path)
    failing = FakeProvider(env.server, fail_before_send=True)
    with pytest.raises(submission.SubmissionUnknown):
        submission.submit(provider=failing, model=env.model, store=env.store, settings=env.settings,
                          catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    intent = env.store.list_intents()[0]
    created = submission._parse(intent["created_at"])

    env.server.create(["zz0000-00000", "zz0000-00001", "zz0000-00002"],
                      created_at=created - timedelta(minutes=5))
    provider = FakeProvider(env.server)
    with env.store.lock():
        messages = submission.reconcile(provider, env.store)
    assert env.store.list_intents()[0]["candidate_batch_id"] is None
    assert any("no batch found yet" in m for m in messages)
