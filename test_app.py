"""End-to-end tests of the upload → metadata → download → delete flow.

R2 is replaced by moto's in-memory S3, which speaks the same API boto3 uses
against R2. No real credentials or network access are needed.
"""

import io
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config
import pytest
import requests
from moto import mock_aws

from app import create_app, limiter
from storage import R2Storage

BUCKET = "test-bucket"
FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture()
def env():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
        client.create_bucket(Bucket=BUCKET)
        storage = R2Storage(client, BUCKET)
        app = create_app({"TESTING": True, "RATELIMIT_ENABLED": False}, storage=storage)
        limiter.enabled = False
        yield app, app.test_client(), client


def upload(client, data=b"hello world", name="report.pdf", headers=FETCH):
    return client.post(
        "/api/upload",
        data={"file": (io.BytesIO(data), name, "application/pdf")},
        headers=headers,
        content_type="multipart/form-data",
    )


def test_homepage_and_legal_pages(env):
    _, c, _ = env
    for path in ("/", "/privacy", "/terms", "/cookies"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert b"<main" in r.data


def test_health(env):
    _, c, _ = env
    r = c.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "healthy"


def test_full_upload_download_flow(env):
    _, c, s3 = env
    payload = b"%PDF-1.4 example bytes"
    r = upload(c, payload, name="Q3 report <final>.pdf")
    assert r.status_code == 201
    body = r.get_json()
    assert body["ok"] is True
    file_id = body["file"]["id"]
    assert len(file_id) == 22
    assert body["file"]["name"] == "Q3 report _final_.pdf"
    assert body["download_url"].endswith(f"/download/{file_id}")
    assert body["delete_token"]

    # Stored privately as an attachment, never as active content.
    head = s3.head_object(Bucket=BUCKET, Key=f"uploads/{file_id}")
    assert head["ContentType"] == "application/octet-stream"
    assert head["ContentDisposition"].startswith("attachment;")
    assert "delete-token-sha256" in head["Metadata"]
    assert body["delete_token"] not in str(head["Metadata"])

    meta = c.get(f"/api/files/{file_id}").get_json()["file"]
    assert meta["size"] == len(payload)
    assert meta["name"] == "Q3 report _final_.pdf"
    assert "delete" not in str(meta).lower()

    r = c.get(f"/download/{file_id}")
    assert r.status_code == 302
    location = r.headers["Location"]
    q = parse_qs(urlparse(location).query)
    assert "X-Amz-Signature" in q and q["X-Amz-Expires"] == ["300"]
    assert q["response-content-disposition"][0].startswith("attachment;")

    # moto intercepts requests made through `requests`, so we can follow the presigned URL.
    assert requests.get(location).content == payload


def test_unicode_filename(env):
    _, c, _ = env
    r = upload(c, name="résumé 履歴書.txt")
    assert r.status_code == 201
    file_id = r.get_json()["file"]["id"]
    assert c.get(f"/api/files/{file_id}").get_json()["file"]["name"] == "résumé 履歴書.txt"


def test_lookup_by_name_returns_signed_url(env):
    _, c, _ = env
    upload(c, b"website bytes", name="Kxznfile").get_json()
    response = c.get("/api/files/by-name/Kxznfile")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["file"]["name"] == "Kxznfile"
    assert payload["file"]["size"] == len(b"website bytes")
    query = parse_qs(urlparse(payload["download_url"]).query)
    assert query["X-Amz-Signature"]
    assert query["X-Amz-Expires"] == ["300"]


def test_lookup_by_name_is_exact_and_missing_is_not_found(env):
    _, c, _ = env
    upload(c, name="Kxznfile").get_json()
    assert c.get("/api/files/by-name/kxznfile").status_code == 404
    assert c.get("/api/files/by-name/missing").status_code == 404


def test_oversized_upload_rejected_by_server(env):
    app, c, _ = env
    app.config["MAX_UPLOAD_BYTES"] = 1024
    r = upload(c, b"x" * 1025)
    assert r.status_code == 413
    assert r.get_json()["error"]["code"] == "too_large"


def test_body_over_content_length_rejected(env):
    app, c, _ = env
    app.config["MAX_CONTENT_LENGTH"] = 2048
    r = upload(c, b"x" * 4096)
    assert r.status_code == 413
    assert r.get_json()["ok"] is False


def test_twenty_mib_limit_is_exact(env):
    app, c, _ = env
    assert app.config["MAX_UPLOAD_BYTES"] == 20 * 1024 * 1024
    assert upload(c, b"\0" * (20 * 1024 * 1024)).status_code == 201
    assert upload(c, b"\0" * (20 * 1024 * 1024 + 1)).status_code == 413


def test_upload_requires_fetch_header_and_file(env):
    _, c, _ = env
    assert upload(c, headers={}).status_code == 400
    r = c.post("/api/upload", data={}, headers=FETCH)
    assert r.get_json()["error"]["code"] == "no_file"
    assert upload(c, b"").get_json()["error"]["code"] == "empty_file"


def test_malformed_ids_and_missing_files(env):
    _, c, _ = env
    assert c.get("/download/../../etc").status_code == 404
    assert c.get("/api/files/short").status_code == 404
    r = c.get("/api/files/" + "A" * 22)
    assert r.status_code == 404 and r.get_json()["error"]["code"] == "not_found"
    assert c.get("/download/" + "A" * 22).status_code == 404


def test_delete_requires_valid_token(env):
    _, c, _ = env
    body = upload(c).get_json()
    fid = body["file"]["id"]
    bad = c.delete(f"/api/files/{fid}", headers={**FETCH, "X-Delete-Token": "nope"})
    assert bad.status_code == 403
    good = c.delete(f"/api/files/{fid}", headers={**FETCH, "X-Delete-Token": body["delete_token"]})
    assert good.status_code == 200
    assert c.get(f"/download/{fid}").status_code == 404


def test_old_file_metadata_remains_available(env):
    app, c, s3 = env
    fid = upload(c).get_json()["file"]["id"]
    s3.copy_object(
        Bucket=BUCKET, Key=f"uploads/{fid}", CopySource={"Bucket": BUCKET, "Key": f"uploads/{fid}"},
        Metadata={"uploaded-at": "2000-01-01T00:00:00+00:00"}, MetadataDirective="REPLACE",
    )
    assert c.get(f"/download/{fid}").status_code == 302
    assert c.get(f"/api/files/{fid}").status_code == 200


def test_upload_replaces_all_previous_files_with_same_name(env):
    _, c, _ = env
    first = upload(c, b"old-1", name="Kxzn.dll").get_json()["file"]["id"]
    second = upload(c, b"old-2", name="Kxzn.dll").get_json()["file"]["id"]
    third = upload(c, b"new", name="Kxzn.dll").get_json()["file"]["id"]

    assert c.get(f"/download/{first}").status_code == 404
    assert c.get(f"/download/{second}").status_code == 404
    assert c.get(f"/download/{third}").status_code == 302


def test_no_overwrite_on_key_collision(env, monkeypatch):
    _, c, s3 = env
    first = upload(c, b"original", name="first.bin").get_json()["file"]["id"]
    ids = iter([first, "B" * 22])
    monkeypatch.setattr("app.secrets.token_urlsafe", lambda n: next(ids) if n == 16 else "tok")
    second = upload(c, b"newer", name="second.bin").get_json()["file"]["id"]
    assert second == "B" * 22
    assert s3.get_object(Bucket=BUCKET, Key=f"uploads/{first}")["Body"].read() == b"original"


def test_security_headers_and_no_cookies(env):
    _, c, _ = env
    r = c.get("/")
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Set-Cookie" not in r.headers


def test_errors_do_not_leak_details(env, monkeypatch):
    app, c, _ = env

    def boom(*a, **k):
        raise RuntimeError("secret internal path /srv/keys")

    monkeypatch.setattr(app.extensions["storage"], "head", boom)
    r = c.get("/api/files/" + "A" * 22)
    assert r.status_code == 500
    assert b"secret" not in r.data and b"Traceback" not in r.data


def test_storage_not_configured(monkeypatch):
    for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        monkeypatch.delenv(v, raising=False)
    app = create_app({"TESTING": True})
    c = app.test_client()
    assert c.get("/").status_code == 200
    r = upload(c)
    assert r.status_code == 503 and r.get_json()["error"]["code"] == "unavailable"


# ------------------------------------------------------------------- admin

PW = "s3cret-admin"


def _csrf(c):
    with c.session_transaction() as s:
        return s.get("csrf", "")


def login(app, c):
    app.config["ADMIN_PASSWORD"] = PW
    c.get("/admin/login")
    c.post("/admin/login", data={"csrf": _csrf(c), "password": PW})


def test_admin_disabled_without_password(env):
    _, c, _ = env  # fixture sets no ADMIN_PASSWORD
    assert c.get("/admin/login").status_code == 404
    assert c.get("/admin").status_code == 404
    assert c.post("/admin/delete/" + "A" * 22, data={}).status_code == 404


def test_admin_requires_login(env):
    app, c, _ = env
    app.config["ADMIN_PASSWORD"] = PW
    r = c.get("/admin")
    assert r.status_code == 302 and "/admin/login" in r.headers["Location"]


def test_admin_login_rejects_wrong_password_and_missing_csrf(env):
    app, c, _ = env
    app.config["ADMIN_PASSWORD"] = PW
    c.get("/admin/login")
    assert c.post("/admin/login", data={"password": PW}).status_code == 400  # no csrf
    r = c.post("/admin/login", data={"csrf": _csrf(c), "password": "wrong"}, follow_redirects=False)
    assert r.status_code == 302
    with c.session_transaction() as s:
        assert not s.get("admin")


def test_admin_lists_and_deletes_any_file(env):
    app, c, _ = env
    a = upload(c, b"aaa", name="first.bin").get_json()["file"]["id"]
    b = upload(c, b"bbbbb", name="second.txt").get_json()["file"]["id"]
    login(app, c)

    page = c.get("/admin")
    assert page.status_code == 200
    body = page.data.decode()
    assert "first.bin" in body and "second.txt" in body
    assert a in body and b in body

    # Admin deletes b without needing its per-file delete key.
    r = c.post(f"/admin/delete/{b}", data={"csrf": _csrf(c)}, follow_redirects=True)
    assert r.status_code == 200
    assert "second.txt" not in r.data.decode()
    assert c.get(f"/download/{b}").status_code == 404
    assert c.get(f"/download/{a}").status_code == 302  # the other file is untouched


def test_admin_delete_needs_csrf(env):
    app, c, _ = env
    fid = upload(c).get_json()["file"]["id"]
    login(app, c)
    assert c.post(f"/admin/delete/{fid}", data={}).status_code == 400
    assert c.get(f"/download/{fid}").status_code == 302  # still there


def test_admin_logout(env):
    app, c, _ = env
    login(app, c)
    assert c.get("/admin").status_code == 200
    c.post("/admin/logout", data={})
    assert c.get("/admin").status_code == 302
