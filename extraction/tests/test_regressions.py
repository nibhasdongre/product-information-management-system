"""Regression tests for two issues found in the real-API runs:
- a page selection like "1-" crashed with a traceback instead of a usage error
- a batch created five minutes before a submission was treated as its candidate
"""
import argparse
from datetime import timedelta

import pytest

from fakes import FakeProvider

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


def test_batch_created_five_minutes_before_the_submission_is_not_a_candidate(env):
    failing = FakeProvider(env.server, fail_before_send=True)
    with pytest.raises(submission.SubmissionUnknown):
        submission.submit(provider=failing, model=env.model, repo=env.repo, settings=env.settings, storage=env.storage,
                          catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    created = env.submissions()[0]["created_at"]

    env.server.create(["zz0000-00000", "zz0000-00001", "zz0000-00002"],
                      created_at=created - timedelta(minutes=5))
    provider = FakeProvider(env.server)
    messages = submission.reconcile(provider, env.repo)
    assert env.submissions()[0]["candidate_batch_id"] is None
    assert any("no batch found yet" in m for m in messages)
