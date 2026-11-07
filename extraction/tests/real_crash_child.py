"""Real-API crash test. Submits pages with the real Claude provider, then exits right
after Anthropic accepts the batch, before anything is recorded locally.

Usage (from pim/extraction, with ANTHROPIC_API_KEY set in pim/.env):
    python tests/real_crash_child.py Turtle --pages 1-3
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.cli import main  # noqa: E402
from pipeline.providers.claude import ClaudeProvider  # noqa: E402

_original_submit_batch = ClaudeProvider.submit_batch


def _crash_after_create(self, requests):
    batch_id = _original_submit_batch(self, requests)
    print(f"batch created: {batch_id}; exiting before anything is recorded locally", flush=True)
    os._exit(9)


ClaudeProvider.submit_batch = _crash_after_create

sys.exit(main(["batch", "submit", *sys.argv[1:]]))
