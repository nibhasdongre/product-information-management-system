"""Batch timing: status records Anthropic's created and ended times, and the batch table
reports the wall-clock time."""
from fakes import FakeProvider

from pipeline import batch, submission
from pipeline.report import batch_wall_time, format_batches


def _submit(env, provider):
    submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings, storage=env.storage,
                      catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)


def test_status_records_batch_timing_and_wall_time(env):
    provider = FakeProvider(env.server)
    _submit(env, provider)
    batch_id = env.server.batch_ids()[0]

    lines = batch.refresh_status(provider, env.repo)
    sub = env.submissions()[0]
    assert sub["batch_created_at"] is not None
    assert sub["batch_ended_at"] is None
    assert batch_wall_time(sub["batch_created_at"], sub["batch_ended_at"]) is None
    assert "wall=" not in lines[0]

    env.server.end(batch_id)
    lines = batch.refresh_status(provider, env.repo)
    sub = env.submissions()[0]
    assert sub["batch_ended_at"] is not None
    wall = batch_wall_time(sub["batch_created_at"], sub["batch_ended_at"])
    assert wall is not None and wall.total_seconds() >= 0
    assert "wall=" in lines[0]

    table = format_batches(env.repo.all_batches())
    assert batch_id in table
    assert str(wall) in table


def test_collect_records_timing_before_writing_results(env):
    provider = FakeProvider(env.server)
    _submit(env, provider)
    env.server.end(env.server.batch_ids()[0])

    assert len(batch.collect(provider, env.repo, env.model)) == 1
    sub = env.submissions()[0]
    assert sub["state"] == "completed"
    assert sub["batch_ended_at"] is not None
    assert batch_wall_time(sub["batch_created_at"], sub["batch_ended_at"]) is not None
