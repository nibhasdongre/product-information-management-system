"""Crash-recovery tests for batch submission (T1-T10). Run from the project root:
    python -m pytest extraction/tests -q
They need DATABASE_URL and run in a throwaway schema (see conftest.py)."""
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from fakes import CRASH_EXIT_CODE, FakeProvider

from pipeline import batch, db, submission
from pipeline.repo import Repo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CHILD = HERE / "crash_child.py"


def submit_now(env, provider):
    return submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings,
                             catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)


def reconcile_now(env, provider, now=None):
    return submission.reconcile(provider, env.repo, now=now)


def failing_submit(env):
    with pytest.raises(submission.SubmissionUnknown):
        submit_now(env, FakeProvider(env.server, fail_before_send=True))


def count(env, table, where="true"):
    return env.q(f"select count(*) as n from {table} where {where}")[0]["n"]


def test_t1_failure_while_reserving_leaves_no_trace_and_pages_stay_ready(env, monkeypatch):
    provider = FakeProvider(env.server)

    def crash(self, submission_id, render_s):
        raise RuntimeError("simulated crash part-way through reserving the pages")

    monkeypatch.setattr(Repo, "_insert_attempts", crash)
    with pytest.raises(RuntimeError):
        submit_now(env, provider)
    monkeypatch.undo()

    assert env.server.batch_ids() == []
    assert count(env, "batch_submissions") == 0
    assert count(env, "page_extractions") == 0
    assert count(env, "extraction_attempts") == 0
    assert reconcile_now(env, provider) == []

    report = submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1
    assert len(report.batch_ids) == 1
    assert env.page_statuses() == ["submitted"] * 3


def test_t2_crash_after_reserving_before_create_is_released_and_resubmitted_once(env, monkeypatch):
    provider = FakeProvider(env.server)
    real_set_state = Repo.set_state

    def crash_before_create(self, submission_id, state, reason=None):
        if state == submission.SUBMITTING:
            raise RuntimeError("simulated crash between reservation and create call")
        return real_set_state(self, submission_id, state, reason)

    monkeypatch.setattr(Repo, "set_state", crash_before_create)
    with pytest.raises(RuntimeError):
        submit_now(env, provider)
    monkeypatch.undo()

    assert env.server.batch_ids() == []
    assert [s["state"] for s in env.submissions()] == [submission.PREPARED]
    messages = reconcile_now(env, provider)
    assert any("never attempted" in m for m in messages)
    assert [s["state"] for s in env.submissions()] == ["released"]
    assert env.page_statuses() == ["released"] * 3
    assert count(env, "extraction_attempts") == 0

    submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1
    assert env.page_statuses() == ["submitted"] * 3


def test_t3_process_dies_right_after_create_is_verified_and_not_resubmitted(env, tmp_path):
    result = subprocess.run([sys.executable, str(CHILD), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == CRASH_EXIT_CODE, result.stderr

    assert len(env.server.batch_ids()) == 1
    sub = env.submissions()[0]
    assert sub["state"] == submission.SUBMITTING
    assert sub["anthropic_batch_id"] is None

    provider = FakeProvider(env.server)
    messages = reconcile_now(env, provider)
    assert any("is processing" in m for m in messages)
    with pytest.raises(submission.UnresolvedSubmissions):
        submit_now(env, provider)

    batch_id = env.server.batch_ids()[0]
    env.server.end(batch_id)
    messages = reconcile_now(env, provider)
    assert any("recorded batch" in m for m in messages)
    assert [s["state"] for s in env.submissions()] == ["submitted"]
    assert env.page_statuses() == ["submitted"] * 3

    assert batch.collect(provider, env.repo, env.model, env.settings) == [batch_id]
    assert env.page_statuses() == ["ok"] * 3
    assert count(env, "extraction_data") == 3
    assert count(env, "product") == 3
    assert len(list((env.settings.output_dir / env.model.name / "Test" / "images").glob("*.png"))) == 3
    assert [s["state"] for s in env.submissions()] == ["completed"]

    report = submit_now(env, provider)
    assert report.batch_ids == []
    assert len(env.server.batch_ids()) == 1


def test_t4_crash_after_create_before_the_batch_is_recorded_is_verified_not_resubmitted(env, monkeypatch):
    provider = FakeProvider(env.server)

    def crash(self, submission_id, batch_id):
        raise RuntimeError("simulated crash after the batch was created, before it was recorded")

    monkeypatch.setattr(Repo, "record_batch", crash)
    with pytest.raises(RuntimeError):
        submit_now(env, provider)
    monkeypatch.undo()

    assert len(env.server.batch_ids()) == 1
    assert [s["state"] for s in env.submissions()] == [submission.SUBMITTING]
    assert env.page_statuses() == ["reserved"] * 3

    reconcile_now(env, provider)
    env.server.end(env.server.batch_ids()[0])
    messages = reconcile_now(env, provider)
    assert any("recorded batch" in m for m in messages)
    assert env.page_statuses() == ["submitted"] * 3

    submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1


def test_t5_recording_a_batch_is_atomic_and_repeatable(env):
    provider = FakeProvider(env.server)
    submit_now(env, provider)
    first = env.submissions()[0]
    assert env.page_statuses() == ["submitted"] * 3

    env.repo.record_batch(first["submission_id"], first["anthropic_batch_id"])  # repeat: no change
    assert env.page_statuses() == ["submitted"] * 3

    other = "00000000-0000-4000-8000-000000000001"
    env.repo.create_submission(other, submission.now_utc(), [
        submission.PageReservation(env.q("select catalog_id from catalog")[0]["catalog_id"], 9, 72, 72, 0.1)])
    with pytest.raises(psycopg.errors.UniqueViolation):
        env.repo.record_batch(other, first["anthropic_batch_id"])  # a batch id belongs to one submission
    assert env.q("select status from page_extractions where page = 9")[0]["status"] == "reserved"
    assert env.q("select state from batch_submissions where submission_id = %s", (other,))[0]["state"] == "prepared"


def test_t6_lost_response_with_no_visible_batch_stays_unresolved_and_never_resubmits(env):
    with pytest.raises(submission.SubmissionUnknown):
        submit_now(env, FakeProvider(env.server, lose_response=True))
    assert len(env.server.batch_ids()) == 1
    created = env.submissions()[0]["created_at"]
    assert env.submissions()[0]["state"] == submission.UNKNOWN

    env.server.set_hide_lists(True)
    provider = FakeProvider(env.server)
    messages = reconcile_now(env, provider, now=created + timedelta(minutes=1))
    assert any("unresolved" in m for m in messages)
    assert env.submissions()[0]["candidate_batch_id"] is None
    with pytest.raises(submission.UnresolvedSubmissions):
        submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1

    env.server.set_hide_lists(False)
    reconcile_now(env, provider, now=created + timedelta(minutes=30))
    candidate = env.submissions()[0]["candidate_batch_id"]
    assert candidate == env.server.batch_ids()[0]

    env.server.end(candidate)
    messages = reconcile_now(env, provider, now=created + timedelta(minutes=31))
    assert any("recorded batch" in m for m in messages)
    assert [s["state"] for s in env.submissions()] == ["submitted"]
    assert env.page_statuses() == ["submitted"] * 3


def test_t7_two_matching_batches_need_review_and_block_submission(env):
    failing_submit(env)
    assert env.server.batch_ids() == []
    env.server.create_unrelated(3)
    env.server.create_unrelated(3)

    provider = FakeProvider(env.server)
    messages = reconcile_now(env, provider)
    assert env.submissions()[0]["state"] == submission.NEEDS_REVIEW
    assert any("NEEDS REVIEW" in m and "2 candidate batches" in m for m in messages)
    assert env.submissions()[0]["anthropic_batch_id"] is None
    with pytest.raises(submission.UnresolvedSubmissions):
        submit_now(env, provider)
    assert env.page_statuses() == ["reserved"] * 3


def test_t8_unrelated_batch_matching_timing_and_count_is_not_trusted(env):
    failing_submit(env)
    unrelated = env.server.create_unrelated(3)

    provider = FakeProvider(env.server)
    reconcile_now(env, provider)
    assert env.submissions()[0]["candidate_batch_id"] == unrelated
    assert env.submissions()[0]["anthropic_batch_id"] is None

    env.server.end(unrelated, results_ids=["other-00001", "other-00002", "other-00003"])
    messages = reconcile_now(env, provider)
    assert any("do not match" in m for m in messages)
    assert env.submissions()[0]["state"] == submission.NEEDS_REVIEW
    assert env.submissions()[0]["anthropic_batch_id"] is None
    assert env.page_statuses() == ["reserved"] * 3


def test_t9_concurrent_batch_command_from_another_process_is_refused_by_the_lock(env):
    probe = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from pipeline import db\n"
        "try:\n"
        "    with db.advisory_lock():\n"
        "        sys.exit(0)\n"
        "except db.LockHeld:\n"
        "    sys.exit(4)\n"
    )
    with db.advisory_lock():
        assert subprocess.run([sys.executable, "-c", probe, str(ROOT)]).returncode == 4
        with pytest.raises(db.LockHeld):
            with db.advisory_lock():
                pass
    assert subprocess.run([sys.executable, "-c", probe, str(ROOT)]).returncode == 0
    assert env.server.batch_ids() == []


def test_t10_unresolved_past_20_hours_escalates_to_review(env):
    failing_submit(env)
    created = env.submissions()[0]["created_at"]
    provider = FakeProvider(env.server)

    messages = reconcile_now(env, provider, now=created + timedelta(hours=19))
    assert any("unresolved" in m for m in messages)
    assert env.submissions()[0]["state"] == submission.UNKNOWN

    messages = reconcile_now(env, provider, now=created + timedelta(hours=21))
    assert any("NEEDS REVIEW" in m and "check the Anthropic console" in m for m in messages)
    assert env.submissions()[0]["state"] == submission.NEEDS_REVIEW
    assert env.page_statuses() == ["reserved"] * 3
