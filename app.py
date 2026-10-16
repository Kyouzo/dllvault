"""Flask application: file uploads to Cloudflare R2 with shareable download links."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

import utils
from storage import ObjectExists, ObjectNotFound, R2Storage, StorageConfigError, StorageError

load_dotenv()  # local development only; on Render, variables come from the dashboard

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

MIB = 1024 * 1024
# Multipart framing (boundaries, part headers) adds a little on top of the file.
MULTIPART_OVERHEAD = 64 * 1024


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, default)))
    except ValueError:
        return default


limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    default_limits=["300 per hour"],
    headers_enabled=True,
)


def create_app(config: dict | None = None, storage: R2Storage | None = None) -> Flask:
    app = Flask(__name__)

    max_bytes = _int_env("MAX_UPLOAD_BYTES", 20 * MIB, 1)
    secret = os.getenv("FLASK_SECRET_KEY")
    if not secret:
        log.warning("FLASK_SECRET_KEY is not set; using a random per-process key.")
        secret = secrets.token_hex(32)

    app.config.update(
        SECRET_KEY=secret,
        MAX_UPLOAD_BYTES=max_bytes,
        # Werkzeug rejects larger request bodies with 413 before we read them.
        MAX_CONTENT_LENGTH=max_bytes + MULTIPART_OVERHEAD,
        RETENTION_DAYS=_int_env("RETENTION_DAYS", 30),
        DOWNLOAD_URL_TTL=_int_env("DOWNLOAD_URL_TTL", 300, 30),
        R2_PUBLIC_BASE_URL=(os.getenv("R2_PUBLIC_BASE_URL") or "").rstrip("/"),
        UPLOAD_RATE_LIMIT=os.getenv("UPLOAD_RATE_LIMIT", "10 per minute;100 per day"),
        SITE_NAME=os.getenv("SITE_NAME", "Fileshelf"),
        OPERATOR_NAME=os.getenv("OPERATOR_NAME", ""),
        CONTACT_EMAIL=os.getenv("CONTACT_EMAIL", ""),
        LEGAL_LAST_UPDATED=os.getenv("LEGAL_LAST_UPDATED", ""),
        JSON_SORT_KEYS=False,
    )
    if config:
        app.config.update(config)

    # Render terminates TLS at its proxy; trust exactly one hop of X-Forwarded-*.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    limiter.init_app(app)

    if storage is None:
        try:
            storage = R2Storage.from_env()
        except StorageConfigError as exc:
            log.error("%s. Uploads are disabled until it is configured.", exc)
    app.extensions["storage"] = storage

    register_routes(app)
    register_errors(app)
    register_security_headers(app)
    return app


# ---------------------------------------------------------------- helpers

def ok(payload: dict, status: int = 200):
    return jsonify({"ok": True, **payload}), status


def fail(status: int, code: str, message: str):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


def get_storage() -> R2Storage:
    from flask import current_app

    storage = current_app.extensions.get("storage")
    if storage is None:
        abort(503, description="File storage is not configured.")
    return storage


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _expiry(uploaded_at: datetime | None, retention_days: int) -> datetime | None:
    if not retention_days or uploaded_at is None:
        return None
    return uploaded_at + timedelta(days=retention_days)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z") if dt else None


def _file_size(stream) -> int:
    pos = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(pos)
    return size


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_live_object(file_id: str):
    """Fetch metadata for a valid, unexpired file or abort with 404/410."""
    from flask import current_app

    if not utils.is_valid_file_id(file_id):
        abort(404)
    storage = get_storage()
    try:
        obj = storage.head(file_id)
    except ObjectNotFound:
        abort(404)

    uploaded_at = _parse_time(obj.metadata.get("uploaded-at")) or obj.last_modified
    expires_at = _expiry(uploaded_at, current_app.config["RETENTION_DAYS"])
    if expires_at and expires_at <= _now():
        try:
            storage.delete(file_id)
        except StorageError:
            pass  # a bucket lifecycle rule will catch it
        abort(410)
    return storage, obj, uploaded_at, expires_at


# ----------------------------------------------------------------- routes

def register_routes(app: Flask) -> None:
    @app.context_processor
    def inject_site():
        cfg = app.config
        return {
            "site_name": cfg["SITE_NAME"],
            "operator_name": cfg["OPERATOR_NAME"],
            "contact_email": cfg["CONTACT_EMAIL"],
            "legal_last_updated": cfg["LEGAL_LAST_UPDATED"],
            "max_upload_bytes": cfg["MAX_UPLOAD_BYTES"],
            "max_upload_mib": cfg["MAX_UPLOAD_BYTES"] // MIB,
            "retention_days": cfg["RETENTION_DAYS"],
            "download_url_ttl": cfg["DOWNLOAD_URL_TTL"],
            "public_delivery": bool(cfg["R2_PUBLIC_BASE_URL"]),
            "current_year": _now().year,
        }

    @app.get("/")
    def index():
        return render_template("index.html", storage_ready=app.extensions.get("storage") is not None)

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.get("/terms")
    def terms():
        return render_template("terms.html")

    @app.get("/cookies")
    def cookies():
        return render_template("cookies.html")

    @app.get("/health")
    @limiter.exempt
    def health():
        # Deliberately does not call R2: Render polls this often.
        return ok({"status": "healthy", "storage_configured": app.extensions.get("storage") is not None})

    @app.post("/api/upload")
    @limiter.limit(lambda: app.config["UPLOAD_RATE_LIMIT"])
    def upload():
        # Custom header: a cross-site HTML form cannot set it, and a cross-site
        # fetch() that sets it needs a CORS preflight, which this app refuses.
        if request.headers.get("X-Requested-With") != "fetch":
            return fail(400, "bad_request", "Uploads must be sent from the upload page.")

        file = request.files.get("file")
        if file is None or not file.filename:
            return fail(400, "no_file", "Choose a file to upload.")
        if len(request.files) > 1:
            return fail(400, "too_many_files", "Upload one file at a time.")

        max_bytes = app.config["MAX_UPLOAD_BYTES"]
        size = _file_size(file.stream)
        if size == 0:
            return fail(400, "empty_file", "That file is empty.")
        if size > max_bytes:
            return fail(413, "too_large", f"Files can be up to {max_bytes // MIB} MiB.")

        name = utils.sanitize_filename(file.filename)
        declared_type = utils.sanitize_mime(file.mimetype)
        uploaded_at = _now().replace(microsecond=0)
        delete_token = secrets.token_urlsafe(24)
        metadata = {
            "original-name": utils.encode_meta(name),
            "declared-type": utils.encode_meta(declared_type),
            "uploaded-at": uploaded_at.isoformat(),
            "delete-token-sha256": _hash_token(delete_token),
        }

        storage = get_storage()
        for _attempt in range(3):
            file_id = secrets.token_urlsafe(16)  # 128 bits, 22 URL-safe chars
            file.stream.seek(0)
            try:
                storage.upload(
                    file_id,
                    file.stream,
                    size,
                    metadata=metadata,
                    content_disposition=utils.content_disposition(name),
                )
                break
            except ObjectExists:
                continue
        else:
            return fail(500, "upload_failed", "The upload could not be saved. Please try again.")

        expires_at = _expiry(uploaded_at, app.config["RETENTION_DAYS"])
        log.info("stored file id=%s size=%d", file_id, size)
        return ok(
            {
                "file": {
                    "id": file_id,
                    "name": name,
                    "size": size,
                    "type": declared_type,
                    "uploaded_at": _iso(uploaded_at),
                    "expires_at": _iso(expires_at),
                },
                "download_url": request.host_url.rstrip("/") + f"/download/{file_id}",
                "delete_token": delete_token,
            },
            201,
        )

    @app.get("/api/files/<file_id>")
    @limiter.limit("60 per minute")
    def file_metadata(file_id: str):
        _storage, obj, uploaded_at, expires_at = _load_live_object(file_id)
        return ok(
            {
                "file": {
                    "id": file_id,
                    "name": utils.decode_meta(obj.metadata.get("original-name")) or "file",
                    "size": obj.size,
                    "type": utils.decode_meta(obj.metadata.get("declared-type")) or "application/octet-stream",
                    "uploaded_at": _iso(uploaded_at),
                    "expires_at": _iso(expires_at),
                }
            }
        )

    @app.delete("/api/files/<file_id>")
    @limiter.limit("20 per minute")
    def delete_file(file_id: str):
        if request.headers.get("X-Requested-With") != "fetch":
            return fail(400, "bad_request", "Invalid request.")
        token = request.headers.get("X-Delete-Token", "")
        storage, obj, *_ = _load_live_object(file_id)
        expected = obj.metadata.get("delete-token-sha256", "")
        if not token or not expected or not hmac.compare_digest(_hash_token(token), expected):
            return fail(403, "forbidden", "That delete key is not valid for this file.")
        storage.delete(file_id)
        log.info("deleted file id=%s", file_id)
        return ok({"deleted": True})

    @app.get("/download/<file_id>")
    @limiter.limit("60 per minute")
    def download(file_id: str):
        storage, obj, *_ = _load_live_object(file_id)
        name = utils.decode_meta(obj.metadata.get("original-name")) or "download"
        public_base = app.config["R2_PUBLIC_BASE_URL"]
        if public_base:
            target = f"{public_base}/{obj.key}"
        else:
            target = storage.presigned_download_url(
                file_id, app.config["DOWNLOAD_URL_TTL"], utils.content_disposition(name)
            )
        resp = redirect(target, code=302)
        resp.headers["Cache-Control"] = "no-store"
        return resp


# ----------------------------------------------------------------- errors

ERROR_COPY = {
    400: ("bad_request", "The request could not be understood."),
    404: ("not_found", "That file doesn't exist or has been deleted."),
    405: ("method_not_allowed", "That action isn't supported here."),
    410: ("expired", "That file has expired and is no longer available."),
    413: ("too_large", None),
    429: ("rate_limited", "Too many requests. Please wait a moment and try again."),
    503: ("unavailable", "File storage is temporarily unavailable."),
}


def register_errors(app: Flask) -> None:
    def wants_json() -> bool:
        return request.path.startswith("/api/") or request.path == "/health"

    def render_error(status: int, code: str, message: str):
        if wants_json():
            return fail(status, code, message)
        return render_template("error.html", status=status, message=message), status

    @app.errorhandler(HTTPException)
    def handle_http(exc: HTTPException):
        code, message = ERROR_COPY.get(exc.code or 500, ("error", "Something went wrong."))
        if exc.code == 413:
            message = f"Files can be up to {app.config['MAX_UPLOAD_BYTES'] // MIB} MiB."
        elif exc.code == 503 and exc.description:
            message = exc.description
        return render_error(exc.code or 500, code, message)

    @app.errorhandler(StorageError)
    def handle_storage(_exc: StorageError):
        return render_error(502, "storage_error", "File storage didn't respond as expected. Please try again.")

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        log.exception("Unhandled error")  # full detail stays in the server log
        return render_error(500, "server_error", "Something went wrong on our side. Please try again.")


def register_security_headers(app: Flask) -> None:
    csp = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.is_secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1", port=_int_env("PORT", 5000))
