"""Crash-recovery tests for batch submission (T1-T10). Run from pim/extraction:
    python -m pytest tests -q
"""
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from fakes import CRASH_EXIT_CODE, FakeProvider, make_env

from pipeline import batch, submission
from pipeline.durable import LockHeld
from pipeline.store import RELEASED, SUBMITTED

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CHILD = HERE / "crash_child.py"
PAGES = (1, 2, 3)


def submit_now(env, provider):
    return submission.submit(provider=provider, model=env.model, store=env.store, settings=env.settings,
                             catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)


def reconcile_now(env, provider, now=None):
    with env.store.lock():
        return submission.reconcile(provider, env.store, now=now)


def page_statuses(env):
    return [env.store.load_page("Test", p)["status"] for p in PAGES]


def failing_submit(env):
    with pytest.raises(submission.SubmissionUnknown):
        submit_now(env, FakeProvider(env.server, fail_before_send=True))


def test_t1_crash_before_intent_leaves_no_trace_and_pages_stay_ready(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    provider = FakeProvider(env.server)

    def crash(intent):
        raise RuntimeError("simulated crash before the intent was written")

    monkeypatch.setattr(env.store, "save_intent", crash)
    with pytest.raises(RuntimeError):
        submit_now(env, provider)
    monkeypatch.undo()

    assert env.server.batch_ids() == []
    assert env.store.list_intents() == []
    messages = reconcile_now(env, provider)
    assert env.store.staged_submission_ids() == []
    assert any("removed images" in m for m in messages)

    report = submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1
    assert len(report.batch_ids) == 1
    assert page_statuses(env) == [SUBMITTED] * 3


def test_t2_crash_after_intent_before_create_is_released_and_resubmitted_once(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    provider = FakeProvider(env.server)
    real_set_state = submission._set_state

    def crash_before_create(store, intent, state, reason=None):
        if state == submission.SUBMITTING:
            raise RuntimeError("simulated crash between intent and create call")
        return real_set_state(store, intent, state, reason)

    monkeypatch.setattr(submission, "_set_state", crash_before_create)
    with pytest.raises(RuntimeError):
        submit_now(env, provider)
    monkeypatch.undo()

    assert env.server.batch_ids() == []
    assert env.store.list_intents()[0]["state"] == submission.PREPARED
    messages = reconcile_now(env, provider)
    assert any("never attempted" in m for m in messages)
    assert env.store.list_intents() == []
    assert page_statuses(env) == [RELEASED] * 3

    submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1


def test_t3_process_dies_right_after_create_is_verified_and_not_resubmitted(tmp_path):
    env = make_env(tmp_path)
    result = subprocess.run([sys.executable, str(CHILD), str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == CRASH_EXIT_CODE, result.stderr

    assert len(env.server.batch_ids()) == 1
    intent = env.store.list_intents()[0]
    assert intent["state"] == submission.SUBMITTING
    assert env.store.manifest_for_submission(intent["submission_id"]) is None

    provider = FakeProvider(env.server)
    messages = reconcile_now(env, provider)
    assert any("is processing" in m for m in messages)
    with pytest.raises(submission.UnresolvedSubmissions):
        submit_now(env, provider)

    batch_id = env.server.batch_ids()[0]
    env.server.end(batch_id)
    messages = reconcile_now(env, provider)
    assert any("recorded batch" in m for m in messages)
    assert env.store.list_intents() == []
    assert page_statuses(env) == [SUBMITTED] * 3

    reconcile_now(env, provider)
    assert env.store.staged_submission_ids() == [intent["submission_id"]]

    with env.store.lock():
        assert batch.collect(provider, env.store, env.model) == [batch_id]
    assert all(env.store.is_done("Test", p) for p in PAGES)
    assert len(list(env.store.catalog_dir("Test").glob("images/*.png"))) == 3

    report = submit_now(env, provider)
    assert report.batch_ids == []
    assert len(env.server.batch_ids()) == 1


def test_t4_crash_after_manifest_is_completed_without_a_new_batch(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    provider = FakeProvider(env.server)
    real_finalize = submission._finalize
    calls = {"count": 0}

    def crash_first_time(store, intent, batch_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash after the manifest was written")
        return real_finalize(store, intent, batch_id)

    monkeypatch.setattr(submission, "_finalize", crash_first_time)
    with pytest.raises(RuntimeError):
        submit_now(env, provider)
    monkeypatch.undo()

    assert len(env.store.list_manifests()) == 1
    assert len(env.store.list_intents()) == 1
    reconcile_now(env, provider)
    assert env.store.list_intents() == []
    assert page_statuses(env) == [SUBMITTED] * 3

    submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1


def test_t5_crash_after_page_states_before_intent_removal_is_recognised(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    provider = FakeProvider(env.server)
    real_delete = env.store.delete_intent
    calls = {"count": 0}

    def crash_first_time(submission_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated crash before the intent was removed")
        real_delete(submission_id)

    monkeypatch.setattr(env.store, "delete_intent", crash_first_time)
    with pytest.raises(RuntimeError):
        submit_now(env, provider)
    monkeypatch.undo()

    assert page_statuses(env) == [SUBMITTED] * 3
    assert len(env.store.list_intents()) == 1
    reconcile_now(env, provider)
    assert env.store.list_intents() == []

    submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1


def test_t6_lost_response_with_no_visible_batch_stays_unresolved_and_never_resubmits(tmp_path):
    env = make_env(tmp_path)
    with pytest.raises(submission.SubmissionUnknown):
        submit_now(env, FakeProvider(env.server, lose_response=True))
    assert len(env.server.batch_ids()) == 1
    created = submission.now_utc()
    intent = env.store.list_intents()[0]
    assert intent["state"] == submission.UNKNOWN

    env.server.set_hide_lists(True)
    provider = FakeProvider(env.server)
    messages = reconcile_now(env, provider, now=created + timedelta(minutes=1))
    assert any("unresolved" in m for m in messages)
    assert env.store.list_intents()[0]["candidate_batch_id"] is None
    with pytest.raises(submission.UnresolvedSubmissions):
        submit_now(env, provider)
    assert len(env.server.batch_ids()) == 1

    env.server.set_hide_lists(False)
    reconcile_now(env, provider, now=created + timedelta(minutes=30))
    candidate = env.store.list_intents()[0]["candidate_batch_id"]
    assert candidate == env.server.batch_ids()[0]

    env.server.end(candidate)
    messages = reconcile_now(env, provider, now=created + timedelta(minutes=31))
    assert any("recorded batch" in m for m in messages)
    assert env.store.list_intents() == []
    assert page_statuses(env) == [SUBMITTED] * 3


def test_t7_two_matching_batches_need_review_and_block_submission(tmp_path):
    env = make_env(tmp_path)
    failing_submit(env)
    assert env.server.batch_ids() == []
    env.server.create_unrelated(3)
    env.server.create_unrelated(3)

    provider = FakeProvider(env.server)
    messages = reconcile_now(env, provider)
    intent = env.store.list_intents()[0]
    assert intent["state"] == submission.NEEDS_REVIEW
    assert any("NEEDS REVIEW" in m and "2 candidate batches" in m for m in messages)
    assert env.store.list_manifests() == []
    with pytest.raises(submission.UnresolvedSubmissions):
        submit_now(env, provider)
    assert all(env.store.is_in_flight("Test", p) for p in PAGES)


def test_t8_unrelated_batch_matching_timing_and_count_is_not_trusted(tmp_path):
    env = make_env(tmp_path)
    failing_submit(env)
    unrelated = env.server.create_unrelated(3)

    provider = FakeProvider(env.server)
    reconcile_now(env, provider)
    assert env.store.list_intents()[0]["candidate_batch_id"] == unrelated
    assert env.store.list_manifests() == []

    env.server.end(unrelated, results_ids=["other-00001", "other-00002", "other-00003"])
    messages = reconcile_now(env, provider)
    assert any("do not match" in m for m in messages)
    assert env.store.list_intents()[0]["state"] == submission.NEEDS_REVIEW
    assert env.store.list_manifests() == []
    assert all(env.store.is_in_flight("Test", p) for p in PAGES)


def test_t9_concurrent_submit_from_another_process_is_refused_by_the_lock(tmp_path):
    env = make_env(tmp_path)
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from pipeline.durable import FileLock, LockHeld\n"
        "try:\n"
        "    with FileLock(Path(sys.argv[2])):\n"
        "        sys.exit(0)\n"
        "except LockHeld:\n"
        "    sys.exit(4)\n"
    )
    with env.store.lock():
        result = subprocess.run([sys.executable, "-c", probe, str(ROOT), str(env.lock_path)])
        assert result.returncode == 4
    assert env.server.batch_ids() == []
    with pytest.raises(LockHeld):
        with env.store.lock():
            with env.store.lock():
                pass


def test_t10_unresolved_past_20_hours_escalates_to_review(tmp_path):
    env = make_env(tmp_path)
    failing_submit(env)
    intent = env.store.list_intents()[0]
    created = submission._parse(intent["created_at"])
    provider = FakeProvider(env.server)

    messages = reconcile_now(env, provider, now=created + timedelta(hours=19))
    assert any("unresolved" in m for m in messages)
    assert env.store.list_intents()[0]["state"] == submission.UNKNOWN

    messages = reconcile_now(env, provider, now=created + timedelta(hours=21))
    assert any("NEEDS REVIEW" in m and "check the Anthropic console" in m for m in messages)
    assert env.store.list_intents()[0]["state"] == submission.NEEDS_REVIEW
    assert all(env.store.is_in_flight("Test", p) for p in PAGES)
