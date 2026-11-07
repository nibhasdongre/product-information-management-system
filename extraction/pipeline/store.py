"""On-disk state under <root>/<model>/:

    <catalog>/pages/pNNN.json        one file per page: its status and results
    <catalog>/images/                 page images written once their result is known
    _submissions/<id>.json            intent for an unresolved submission
    _submissions/<id>/images/         page renders staged until their batch is collected
    _submissions/.lock                held by every batch command
    _batches/<batch_id>.json          manifest for a batch the pipeline has recorded

Every write goes through durable.write_atomic, so a crash never leaves a half-written file."""
from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from pipeline.durable import FileLock, write_atomic
from pipeline.images import slug

logger = logging.getLogger(__name__)

RESERVED = "reserved"     # belongs to an unresolved or recorded submission; never sent again
SUBMITTED = "submitted"   # its batch id is recorded
RELEASED = "released"     # its submission was abandoned before the create call; eligible again
_IN_FLIGHT = (RESERVED, SUBMITTED)


def _remove_tree(path: Path) -> None:
    """Delete a staging folder, logging every file that could not be removed."""
    def report(exc: BaseException, target: str) -> None:
        logger.warning("could not delete staged file %s: %s", target, exc)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda func, target, exc: report(exc, target))
    else:
        shutil.rmtree(path, onerror=lambda func, target, exc_info: report(exc_info[1], target))


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _dump(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


class OutputStore:
    def __init__(self, root: Path, model: str):
        self._root = root / model

    def catalog_dir(self, catalog: str) -> Path:
        return self._root / slug(catalog, 80)

    def _page_path(self, catalog: str, page: int) -> Path:
        return self.catalog_dir(catalog) / "pages" / f"p{page:03d}.json"

    def image_path(self, catalog: str, filename: str) -> Path:
        return self.catalog_dir(catalog) / "images" / filename

    def load_page(self, catalog: str, page: int) -> dict[str, Any] | None:
        return _read_json(self._page_path(catalog, page))

    def is_done(self, catalog: str, page: int) -> bool:
        record = self.load_page(catalog, page)
        return record is not None and record.get("status") == "ok"

    def is_in_flight(self, catalog: str, page: int) -> bool:
        record = self.load_page(catalog, page)
        return record is not None and record.get("status") in _IN_FLIGHT

    def iter_pages(self, catalog: str) -> list[dict[str, Any]]:
        pages_dir = self.catalog_dir(catalog) / "pages"
        if not pages_dir.is_dir():
            return []
        records = (_read_json(path) for path in sorted(pages_dir.glob("p*.json")))
        return [record for record in records if record is not None]

    def save_page(self, catalog: str, page: int, record: dict[str, Any]) -> None:
        write_atomic(self._page_path(catalog, page), _dump(record))

    def save_image(self, catalog: str, filename: str, png: bytes) -> None:
        path = self.image_path(catalog, filename)
        if not path.exists():
            write_atomic(path, png)

    def save_summary(self, catalog: str, summary: dict[str, Any]) -> None:
        write_atomic(self.catalog_dir(catalog) / "summary.json", _dump(summary))

    # Submissions: intents, staged images, and the lock.

    def _submissions_dir(self) -> Path:
        return self._root / "_submissions"

    def lock(self) -> FileLock:
        return FileLock(self._submissions_dir() / ".lock")

    def _intent_path(self, submission_id: str) -> Path:
        return self._submissions_dir() / f"{submission_id}.json"

    def _staging_dir(self, submission_id: str) -> Path:
        return self._submissions_dir() / submission_id

    def save_intent(self, intent: dict[str, Any]) -> None:
        write_atomic(self._intent_path(intent["submission_id"]), _dump(intent))

    def load_intent(self, submission_id: str) -> dict[str, Any] | None:
        return _read_json(self._intent_path(submission_id))

    def list_intents(self) -> list[dict[str, Any]]:
        directory = self._submissions_dir()
        if not directory.is_dir():
            return []
        intents = (_read_json(path) for path in sorted(directory.glob("*.json")))
        return [intent for intent in intents if intent is not None]

    def delete_intent(self, submission_id: str) -> None:
        self._intent_path(submission_id).unlink(missing_ok=True)

    def stage_image(self, submission_id: str, custom_id: str, png: bytes) -> None:
        write_atomic(self._staging_dir(submission_id) / "images" / f"{custom_id}.png", png)

    def load_staged(self, submission_id: str, custom_id: str) -> bytes | None:
        path = self._staging_dir(submission_id) / "images" / f"{custom_id}.png"
        return path.read_bytes() if path.exists() else None

    def drop_staged(self, submission_id: str) -> None:
        path = self._staging_dir(submission_id)
        if path.exists():
            _remove_tree(path)

    def staged_submission_ids(self) -> list[str]:
        directory = self._submissions_dir()
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.iterdir() if path.is_dir())

    # Batches: manifests recorded by the pipeline.

    def _batches_dir(self) -> Path:
        return self._root / "_batches"

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        write_atomic(self._batches_dir() / f"{manifest['batch_id']}.json", _dump(manifest))

    def list_manifests(self) -> list[dict[str, Any]]:
        batches_dir = self._batches_dir()
        if not batches_dir.is_dir():
            return []
        manifests = (_read_json(path) for path in sorted(batches_dir.glob("*.json")))
        return [m for m in manifests if m is not None]

    def known_batch_ids(self) -> set[str]:
        return {m["batch_id"] for m in self.list_manifests()}

    def manifest_for_submission(self, submission_id: str) -> dict[str, Any] | None:
        for manifest in self.list_manifests():
            if manifest.get("submission_id") == submission_id:
                return manifest
        return None
