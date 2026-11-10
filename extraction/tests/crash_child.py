"""Runs one submission in its own process, which exits right after the batch is created.
Usage: python crash_child.py <base_dir>   (DATABASE_URL and PIM_DB_SCHEMA come from the environment)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeProvider, make_env  # noqa: E402

from pipeline import submission  # noqa: E402

env = make_env(Path(sys.argv[1]))
provider = FakeProvider(env.server, crash_after_create=True)
submission.submit(provider=provider, model=env.model, repo=env.repo, settings=env.settings,
                  catalog_dir=env.catalog_dir, catalogs=["Test"], pages=None)
print("submission returned; the crash should have happened first", file=sys.stderr)
