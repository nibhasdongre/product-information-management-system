"""Real-API manual release test. The process exits just before the create call, so no batch
is ever created. The pipeline then cannot find a batch, and an operator must release the
submission by hand with `batch resolve <id> --release --confirm-no-batch`.

Usage (from pim/extraction, with ANTHROPIC_API_KEY set in pim/.env):
    python tests/real_release_child.py Turtle --pages 1-3
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.cli import main  # noqa: E402
from pipeline.providers.claude import ClaudeProvider  # noqa: E402


def _exit_before_create(self, requests):
    print("exiting before the create call; no batch will be created", flush=True)
    os._exit(9)


ClaudeProvider.submit_batch = _exit_before_create

sys.exit(main(["batch", "submit", *sys.argv[1:]]))
