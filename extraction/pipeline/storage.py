"""Object storage for catalogue PDFs and page images.

Keys are the same on every backend:
    pdfs/<sha256>.pdf                  the source catalogue (content-addressed, so a re-upload is the same object)

Two backends: Supabase Storage (REST, private bucket, service key) and a local folder for tests and
for working offline. Uploads overwrite (upsert), so every write is safe to repeat; that is why storage
calls are retried while the Anthropic batch create call is not."""
from __future__ import annotations

import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import httpx

from pipeline.durable import write_atomic

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "catalogs"


class StorageError(RuntimeError):
    """A storage call failed (after retries, where retrying applies)."""


class StorageNotFound(StorageError):
    """The object does not exist."""


def pdf_key(sha256: str) -> str:
    return f"pdfs/{sha256}.pdf"


class Storage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> None:
        """Write the object, replacing any existing one."""

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Return the object's bytes. Raises StorageNotFound if it does not exist."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the object. Not an error if it is already gone."""

    @abstractmethod
    def describe(self) -> str:
        ...

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        self.put(key, path.read_bytes(), content_type)

    def check(self) -> None:
        """Write, read back and delete a probe object; raises StorageError if any step fails."""
        key, data = f"probe/{uuid.uuid4().hex}.txt", b"pim storage check"
        self.put(key, data, "text/plain")
        if self.get(key) != data:
            raise StorageError("the probe object read back different from what was written")
        self.delete(key)


class LocalStorage(Storage):
    def __init__(self, root: Path):
        self._root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if self._root.resolve() not in path.parents:
            raise StorageError(f"invalid storage key {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        write_atomic(self._path(key), data)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise StorageNotFound(key) from None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def describe(self) -> str:
        return f"local folder {self._root}"


class SupabaseStorage(Storage):
    def __init__(self, url: str, secret_key: str, bucket: str = DEFAULT_BUCKET, *,
                 client: httpx.Client | None = None, tries: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self._url = url.rstrip("/")
        self._base = f"{self._url}/storage/v1"
        self._bucket = bucket
        # New-style secret keys (sb_secret_...) are not JWTs: send them as `apikey` only, or Storage
        # treats the call as anonymous. The older service_role key is a JWT and also goes as Bearer.
        self._headers = {"apikey": secret_key}
        if not secret_key.startswith("sb_"):
            self._headers["Authorization"] = f"Bearer {secret_key}"
        self._client = client or httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0))
        self._tries = tries
        self._sleep = sleep

    def _request(self, method: str, path: str, *, headers: dict[str, str] | None = None, **kwargs) -> httpx.Response:
        """Retry transport errors, 429 and 5xx. Safe because every storage write is an overwrite."""
        last: Exception | None = None
        for attempt in range(1, self._tries + 1):
            try:
                response = self._client.request(method, self._base + path,
                                                headers={**self._headers, **(headers or {})}, **kwargs)
            except httpx.TransportError as exc:
                last = exc
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                last = StorageError(f"HTTP {response.status_code}")
            if attempt < self._tries:
                logger.warning("storage %s %s failed (%s); retrying", method, path, last)
                self._sleep(2 ** (attempt - 1))
        raise StorageError(f"{method} {path} failed after {self._tries} tries: {last}") from last

    def _object(self, key: str) -> str:
        return f"{quote(self._bucket, safe='')}/{quote(key, safe='/')}"

    @staticmethod
    def _missing(response: httpx.Response) -> bool:
        # Storage reports a missing object or bucket as 404, or as 400 with a "not found" body.
        return response.status_code == 404 or (response.status_code == 400 and "not found" in response.text.lower())

    @staticmethod
    def _fail(what: str, response: httpx.Response) -> StorageError:
        return StorageError(f"{what}: HTTP {response.status_code} {response.text[:200]}")

    def put(self, key: str, data: bytes, content_type: str) -> None:
        response = self._request("POST", f"/object/{self._object(key)}", content=data,
                                 headers={"x-upsert": "true", "Content-Type": content_type})
        if response.status_code >= 400:
            raise self._fail(f"upload {key}", response)

    def get(self, key: str) -> bytes:
        response = self._request("GET", f"/object/authenticated/{self._object(key)}")
        if self._missing(response):
            raise StorageNotFound(key)
        if response.status_code >= 400:
            raise self._fail(f"download {key}", response)
        return response.content

    def delete(self, key: str) -> None:
        response = self._request("DELETE", f"/object/{self._object(key)}")
        if response.status_code >= 400 and not self._missing(response):
            raise self._fail(f"delete {key}", response)

    def ensure_bucket(self) -> bool:
        """Create the private bucket if it does not exist. Returns True if it was created."""
        response = self._request("GET", f"/bucket/{quote(self._bucket, safe='')}")
        if response.status_code < 400:
            return False
        if not self._missing(response):
            raise self._fail("look up bucket", response)
        created = self._request("POST", "/bucket", json={"id": self._bucket, "name": self._bucket, "public": False})
        if created.status_code == 409:
            return False
        if created.status_code >= 400:
            raise self._fail(f"create bucket {self._bucket!r}", created)
        return True

    def check(self) -> None:
        if self.ensure_bucket():
            logger.info("created private bucket %r", self._bucket)
        super().check()

    def describe(self) -> str:
        return f"Supabase Storage bucket '{self._bucket}' at {self._url}"


def build_storage(output_dir: Path) -> Storage:
    """Supabase Storage when SUPABASE_URL and SUPABASE_SECRET_KEY are both set, else a local folder."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if url and key:
        return SupabaseStorage(url, key, os.environ.get("SUPABASE_STORAGE_BUCKET", "").strip() or DEFAULT_BUCKET)
    if url or key:
        raise StorageError("set both SUPABASE_URL and SUPABASE_SECRET_KEY, or neither to use local storage")
    return LocalStorage(Path(output_dir) / "storage")
