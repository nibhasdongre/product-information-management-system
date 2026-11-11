"""Storage backends. No database and no network: the Supabase backend runs against
httpx's mock transport, so these tests pin down the requests it sends, not Supabase's behaviour. Run
`python -m pipeline storage check` against the real project to verify that."""
import json

import httpx
import pytest

from pipeline.storage import (
    LocalStorage,
    StorageError,
    StorageNotFound,
    SupabaseStorage,
    build_storage,
    pdf_key,
)


# Keys

def test_pdf_keys_are_content_addressed_and_stable():
    assert pdf_key("ab" * 32) == f"pdfs/{'ab' * 32}.pdf"


# Local backend

def test_local_storage_round_trip_overwrite_and_delete(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    storage.put("pdfs/a.pdf", b"one", "application/pdf")
    storage.put("pdfs/a.pdf", b"two", "application/pdf")            # overwrite is allowed
    assert storage.get("pdfs/a.pdf") == b"two"
    storage.delete("pdfs/a.pdf")
    storage.delete("pdfs/a.pdf")                                # deleting twice is fine
    with pytest.raises(StorageNotFound):
        storage.get("pdfs/a.pdf")


def test_local_storage_refuses_keys_that_escape_its_folder(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    with pytest.raises(StorageError):
        storage.put("../outside.txt", b"x", "text/plain")
    assert not (tmp_path / "outside.txt").exists()


def test_check_passes_on_a_working_backend_and_leaves_nothing_behind(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    storage.check()
    assert not any((tmp_path / "store").rglob("*.txt"))


# Supabase backend, against a mock transport

class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(handler, tries=3):
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return SupabaseStorage("https://abc.supabase.co/", "s3cret", "catalogs", client=client, tries=tries,
                           sleep=sleeps.append), sleeps


def ok(content=None, status=200, **kw):
    if content is not None:
        kw["content"] = content
    return httpx.Response(status, **kw)


def test_upload_is_an_authenticated_overwriting_post_to_the_private_bucket():
    handler = Recorder(ok(json={"Key": "catalogs/pdfs/x.pdf"}))
    storage, _ = make(handler)
    storage.put("pdfs/x.pdf", b"%PDF-data", "application/pdf")
    request = handler.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://abc.supabase.co/storage/v1/object/catalogs/pdfs/x.pdf"
    assert request.headers["apikey"] == "s3cret" and request.headers["authorization"] == "Bearer s3cret"
    assert request.headers["x-upsert"] == "true" and request.headers["content-type"] == "application/pdf"
    assert request.content == b"%PDF-data"


def test_download_uses_the_authenticated_path_for_private_buckets():
    handler = Recorder(ok(b"pdf-bytes"))
    storage, _ = make(handler)
    assert storage.get("pdfs/c.pdf") == b"pdf-bytes"
    assert handler.requests[0].method == "GET"
    assert handler.requests[0].url.path == "/storage/v1/object/authenticated/catalogs/pdfs/c.pdf"


@pytest.mark.parametrize("response", [
    ok(status=404, json={"message": "Object not found"}),
    ok(status=400, json={"error": "not_found", "message": "Object not found"}),
])
def test_missing_object_is_not_found_however_storage_reports_it(response):
    storage, _ = make(Recorder(response))
    with pytest.raises(StorageNotFound):
        storage.get("pdfs/i.pdf")


def test_transient_failures_are_retried_with_backoff_then_succeed():
    handler = Recorder(ok(status=503), httpx.ConnectError("reset"), ok(json={}))
    storage, sleeps = make(handler)
    storage.put("pdfs/x.pdf", b"data", "application/pdf")
    assert len(handler.requests) == 3 and sleeps == [1, 2]


def test_retries_give_up_after_the_limit():
    handler = Recorder(ok(status=500))
    storage, sleeps = make(handler)
    with pytest.raises(StorageError, match="after 3 tries"):
        storage.put("pdfs/x.pdf", b"data", "application/pdf")
    assert len(handler.requests) == 3 and sleeps == [1, 2]


def test_a_rejected_request_is_not_retried():
    handler = Recorder(ok(status=403, json={"message": "new row violates row-level security policy"}))
    storage, sleeps = make(handler)
    with pytest.raises(StorageError, match="403"):
        storage.put("pdfs/x.pdf", b"data", "application/pdf")
    assert len(handler.requests) == 1 and sleeps == []


def test_ensure_bucket_creates_a_private_bucket_only_when_missing():
    handler = Recorder(ok(status=404, json={"message": "Bucket not found"}), ok(json={"name": "catalogs"}))
    storage, _ = make(handler)
    assert storage.ensure_bucket() is True
    get, post = handler.requests
    assert get.url.path == "/storage/v1/bucket/catalogs"
    assert post.method == "POST" and post.url.path == "/storage/v1/bucket"
    assert json.loads(post.content) == {"id": "catalogs", "name": "catalogs", "public": False}

    existing = Recorder(ok(json={"id": "catalogs", "public": False}))
    storage, _ = make(existing)
    assert storage.ensure_bucket() is False and len(existing.requests) == 1

    raced = Recorder(ok(status=404), ok(status=409))
    storage, _ = make(raced)
    assert storage.ensure_bucket() is False                      # someone else created it first


def test_check_runs_against_a_fake_server_and_leaves_no_object():
    objects: dict[str, bytes] = {}

    def server(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/storage/v1/bucket/catalogs":
            return ok(json={"id": "catalogs"})
        key = path.split("catalogs/", 1)[1]
        if request.method == "POST":
            objects[key] = request.content
            return ok(json={})
        if request.method == "GET":
            return ok(objects[key]) if key in objects else ok(status=404)
        objects.pop(key, None)
        return ok(json={})

    storage, _ = make(server)
    storage.check()
    assert objects == {}


# Backend selection

def test_build_storage_picks_the_backend_from_the_environment(monkeypatch, tmp_path):
    for name in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_STORAGE_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(build_storage(tmp_path), LocalStorage)

    monkeypatch.setenv("SUPABASE_URL", "https://abc.supabase.co")
    with pytest.raises(StorageError, match="both"):               # half-configured must not fall back silently
        build_storage(tmp_path)

    monkeypatch.setenv("SUPABASE_SECRET_KEY", "s3cret")
    storage = build_storage(tmp_path)
    assert isinstance(storage, SupabaseStorage) and "catalogs" in storage.describe()
    monkeypatch.setenv("SUPABASE_STORAGE_BUCKET", "other")
    assert "other" in build_storage(tmp_path).describe()
