"""Cloudflare R2 storage service.

All object-storage operations live here so route handlers never touch boto3
directly. R2 speaks the S3 API, so we use a standard boto3 S3 client pointed
at the account endpoint: https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)

REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")


class StorageError(Exception):
    """Generic, safe-to-surface storage failure (details go to the log only)."""


class StorageConfigError(StorageError):
    """Required configuration is missing."""


class ObjectNotFound(StorageError):
    """The requested object does not exist."""


class ObjectExists(StorageError):
    """A write was refused because the key is already taken."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    last_modified: datetime | None
    metadata: dict[str, str]


class R2Storage:
    def __init__(self, client, bucket: str, key_prefix: str = "uploads/") -> None:
        self._client = client
        self.bucket = bucket
        self.key_prefix = key_prefix

    # ------------------------------------------------------------------ setup
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "R2Storage":
        env = os.environ if env is None else env
        missing = [name for name in REQUIRED_ENV if not env.get(name)]
        if missing:
            raise StorageConfigError("Missing R2 configuration: " + ", ".join(missing))

        client = boto3.client(
            "s3",
            endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=env["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                # boto3 >= 1.36 adds CRC checksums by default; only send them
                # when an operation requires it, which is what R2 expects.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )
        return cls(client, env["R2_BUCKET_NAME"])

    def key_for(self, file_id: str) -> str:
        return f"{self.key_prefix}{file_id}"

    # ------------------------------------------------------------- operations
    def upload(
        self,
        file_id: str,
        fileobj: BinaryIO,
        size: int,
        *,
        metadata: dict[str, str],
        content_disposition: str,
    ) -> None:
        """Store an object without ever overwriting an existing key.

        Objects are always stored as application/octet-stream with an
        `attachment` disposition so browsers download rather than render them.
        """
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self.key_for(file_id),
                Body=fileobj,
                ContentLength=size,
                ContentType="application/octet-stream",
                ContentDisposition=content_disposition,
                Metadata=metadata,
                IfNoneMatch="*",  # conditional write: fail if the key exists
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412):
                raise ObjectExists(file_id) from exc
            log.exception("R2 upload failed")
            raise StorageError("upload failed") from exc
        except BotoCoreError as exc:
            log.exception("R2 upload failed")
            raise StorageError("upload failed") from exc

    def head(self, file_id: str) -> StoredObject:
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=self.key_for(file_id))
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code", "")
            if status == 404 or code in ("404", "NoSuchKey", "NotFound"):
                raise ObjectNotFound(file_id) from exc
            log.exception("R2 head failed")
            raise StorageError("lookup failed") from exc
        except BotoCoreError as exc:
            log.exception("R2 head failed")
            raise StorageError("lookup failed") from exc
        return StoredObject(
            key=self.key_for(file_id),
            size=int(resp.get("ContentLength", 0)),
            last_modified=resp.get("LastModified"),
            metadata={k.lower(): v for k, v in (resp.get("Metadata") or {}).items()},
        )

    def presigned_download_url(self, file_id: str, ttl_seconds: int, content_disposition: str) -> str:
        try:
            return self._client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": self.key_for(file_id),
                    "ResponseContentDisposition": content_disposition,
                    "ResponseContentType": "application/octet-stream",
                },
                ExpiresIn=ttl_seconds,
            )
        except (ClientError, BotoCoreError) as exc:
            log.exception("R2 presign failed")
            raise StorageError("could not create download link") from exc

    def delete(self, file_id: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self.key_for(file_id))
        except (ClientError, BotoCoreError) as exc:
            log.exception("R2 delete failed")
            raise StorageError("delete failed") from exc

    def list_ids(self, limit: int = 2000) -> list[tuple[str, int, "datetime | None"]]:
        """Return (file_id, size, last_modified) for every stored object.

        Cheap: one or more list calls, no per-object HEAD. Used to drive the
        admin listing, which then fetches names lazily via `head`.
        """
        results: list[tuple[str, int, datetime | None]] = []
        token: str | None = None
        prefix_len = len(self.key_prefix)
        try:
            while len(results) < limit:
                kwargs = {"Bucket": self.bucket, "Prefix": self.key_prefix, "MaxKeys": 1000}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = self._client.list_objects_v2(**kwargs)
                for item in resp.get("Contents", []):
                    file_id = item["Key"][prefix_len:]
                    if file_id:
                        results.append((file_id, int(item.get("Size", 0)), item.get("LastModified")))
                if resp.get("IsTruncated") and resp.get("NextContinuationToken"):
                    token = resp["NextContinuationToken"]
                else:
                    break
        except (ClientError, BotoCoreError) as exc:
            log.exception("R2 list failed")
            raise StorageError("could not list files") from exc
        return results

    def head_optional(self, file_id: str) -> StoredObject | None:
        """Like `head`, but returns None instead of raising when absent."""
        try:
            return self.head(file_id)
        except ObjectNotFound:
            return None

    def find_by_original_name(self, name: str) -> tuple[str, StoredObject] | None:
        """Find the first object whose stored original filename matches exactly."""
        matches = self.find_all_by_original_name(name, limit=1)
        return matches[0] if matches else None

    def find_all_by_original_name(self, name: str, limit: int | None = None) -> list[tuple[str, StoredObject]]:
        """Find stored objects whose original filename matches exactly."""
        matches: list[tuple[str, StoredObject]] = []
        for file_id, _size, _last_modified in self.list_ids():
            obj = self.head_optional(file_id)
            if obj and obj.metadata.get("original-name"):
                # Metadata is encoded by utils.encode_meta; import lazily to
                # keep this storage module independent of Flask application code.
                from utils import decode_meta

                if decode_meta(obj.metadata["original-name"]) == name:
                    matches.append((file_id, obj))
                    if limit is not None and len(matches) >= limit:
                        break
        return matches
