"""Batch timing: status records Anthropic's created and ended times, and the batch table
reports the wall-clock time. Run from pim/extraction with the rest of the tests."""
from fakes import FakeProvider, make_env

from pipeline import batch, submission
from pipeline.report import batch_wall_time, format_batches


def test_status_records_batch_timing_and_wall_time(tmp_path):
    env = make_env(tmp_path)
    provider = FakeProvider(env.server)
    submission.submit(provider=provider, model=env.model, store=env.store, settings=env.settings,
                      catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    batch_id = env.server.batch_ids()[0]

    with env.store.lock():
        lines = batch.refresh_status(provider, env.store)
    manifest = env.store.list_manifests()[0]
    assert manifest["batch_created_at"] is not None
    assert manifest["batch_ended_at"] is None
    assert batch_wall_time(manifest) is None
    assert "wall=" not in lines[0]

    env.server.end(batch_id)
    with env.store.lock():
        lines = batch.refresh_status(provider, env.store)
    manifest = env.store.list_manifests()[0]
    assert manifest["batch_ended_at"] is not None
    wall = batch_wall_time(manifest)
    assert wall is not None and wall.total_seconds() >= 0
    assert "wall=" in lines[0]

    table = format_batches(env.store.list_manifests())
    assert batch_id in table
    assert str(wall) in table


def test_collect_records_timing_before_writing_results(tmp_path):
    env = make_env(tmp_path)
    provider = FakeProvider(env.server)
    submission.submit(provider=provider, model=env.model, store=env.store, settings=env.settings,
                      catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
    env.server.end(env.server.batch_ids()[0])

    with env.store.lock():
        assert len(batch.collect(provider, env.store, env.model)) == 1
    manifest = env.store.list_manifests()[0]
    assert manifest["collected"] is True
    assert manifest["batch_ended_at"] is not None
    assert batch_wall_time(manifest) is not None
