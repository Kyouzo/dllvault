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
