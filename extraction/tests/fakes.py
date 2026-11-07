"""In-memory stand-in for the Batch API. Its state lives in a JSON file, so a child
process can create a batch and die, and the parent still sees that batch afterwards."""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from pipeline.config import ModelSpec, RetrySettings, Settings  # noqa: E402
from pipeline.providers.base import (  # noqa: E402
    BatchResult,
    BatchStatus,
    BatchSummary,
    PageExtraction,
    Provider,
)
from pipeline.store import OutputStore  # noqa: E402

CRASH_EXIT_CODE = 7


class FakeServer:
    def __init__(self, path: Path):
        self.path = path
        if not path.exists():
            self._save({"batches": {}, "hide_lists": False})

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, state: dict) -> None:
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def create(self, custom_ids: list[str], created_at: datetime | None = None) -> str:
        state = self._load()
        batch_id = f"msgbatch_{uuid.uuid4().hex[:12]}"
        state["batches"][batch_id] = {
            "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
            "custom_ids": list(custom_ids),
            "results_ids": list(custom_ids),
            "ended": False,
            "ended_at": None,
        }
        self._save(state)
        return batch_id

    def create_unrelated(self, count: int) -> str:
        return self.create([f"zz{uuid.uuid4().hex[:6]}-{i:05d}" for i in range(count)])

    def end(self, batch_id: str, results_ids: list[str] | None = None) -> None:
        state = self._load()
        batch = state["batches"][batch_id]
        batch["ended"] = True
        batch["ended_at"] = datetime.now(timezone.utc).isoformat()
        if results_ids is not None:
            batch["results_ids"] = list(results_ids)
        self._save(state)

    def batch(self, batch_id: str) -> dict:
        return self._load()["batches"][batch_id]

    def batch_ids(self) -> list[str]:
        return list(self._load()["batches"])

    def batches_with_data(self) -> list[tuple[str, dict]]:
        return list(self._load()["batches"].items())

    def set_hide_lists(self, hidden: bool) -> None:
        state = self._load()
        state["hide_lists"] = hidden
        self._save(state)

    def hide_lists(self) -> bool:
        return self._load()["hide_lists"]


class FakeProvider(Provider):
    def __init__(self, server: FakeServer, *, crash_after_create: bool = False,
                 lose_response: bool = False, fail_before_send: bool = False):
        self.server = server
        self.crash_after_create = crash_after_create
        self.lose_response = lose_response
        self.fail_before_send = fail_before_send

    def build_request(self, png: bytes) -> dict:
        return {"image_bytes": len(png)}

    def submit_batch(self, requests: list[dict]) -> str:
        if self.fail_before_send:
            raise ConnectionError("connection reset before the request was sent")
        batch_id = self.server.create([r["custom_id"] for r in requests])
        if self.crash_after_create:
            os._exit(CRASH_EXIT_CODE)
        if self.lose_response:
            raise ConnectionError("response lost after the batch was created")
        return batch_id

    def batch_status(self, batch_id: str) -> BatchStatus:
        batch = self.server.batch(batch_id)
        n = len(batch["custom_ids"])
        created = datetime.fromisoformat(batch["created_at"])
        ended = datetime.fromisoformat(batch["ended_at"]) if batch.get("ended_at") else None
        if batch["ended"]:
            return BatchStatus("ended", {"processing": 0, "succeeded": n, "errored": 0,
                                         "canceled": 0, "expired": 0}, created, ended)
        return BatchStatus("in_progress", {"processing": n, "succeeded": 0, "errored": 0,
                                           "canceled": 0, "expired": 0}, created, None)

    def list_batches(self, since: datetime) -> list[BatchSummary]:
        if self.server.hide_lists():
            return []
        found = []
        for batch_id, batch in self.server.batches_with_data():
            created = datetime.fromisoformat(batch["created_at"])
            if created >= since:
                status = "ended" if batch["ended"] else "in_progress"
                found.append(BatchSummary(batch_id, created, len(batch["custom_ids"]), status))
        return sorted(found, key=lambda b: b.created_at, reverse=True)

    def batch_results(self, batch_id: str) -> list[BatchResult]:
        batch = self.server.batch(batch_id)
        if not batch["ended"]:
            return []
        message = SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
                                  stop_reason="end_turn")
        return [BatchResult(cid, "succeeded", message=message) for cid in batch["results_ids"]]

    def parse_result(self, message, metrics) -> PageExtraction:
        return PageExtraction(page_type="product", products=[{"product_name": "Product"}], metrics=metrics)

    def extract_page(self, png: bytes) -> PageExtraction:
        raise NotImplementedError("the synchronous path is not used in these tests")


def make_pdf(path: Path, pages: int) -> None:
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"Product {number}")
    doc.save(str(path))
    doc.close()


@dataclass
class Env:
    settings: Settings
    model: ModelSpec
    store: OutputStore
    server: FakeServer
    catalog_dir: Path

    @property
    def lock_path(self) -> Path:
        return self.store._submissions_dir() / ".lock"


def make_env(base: Path, pages: int = 3) -> Env:
    catalog_dir = base / "catalogs"
    output_dir = base / "runs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    make_pdf(catalog_dir / "Test.pdf", pages)
    settings = Settings(
        catalog_dir=catalog_dir,
        output_dir=output_dir,
        render_dpi=72,
        default_model="claude-sonnet-5",
        retry=RetrySettings(tries=1, base_s=0.0, cap_s=0.0),
        batch_max_requests=10_000,
        batch_max_bytes=200 * 1024 * 1024,
    )
    model = ModelSpec(name="claude-sonnet-5", provider="claude", max_output_tokens=8192,
                      input_usd_per_mtok=2.0, output_usd_per_mtok=10.0, batch_discount=0.5)
    store = OutputStore(output_dir, model.name)
    server = FakeServer(base / "server.json")
    return Env(settings, model, store, server, catalog_dir)
