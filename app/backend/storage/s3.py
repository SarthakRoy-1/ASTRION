"""An S3-compatible object store: Supabase Storage, or any other S3 endpoint.

Supabase exposes its storage through an S3-compatible endpoint
(`https://<project>.storage.supabase.co/storage/v1/s3`) with access keys created
in its dashboard, so the same code serves AWS S3, Cloudflare R2, Backblaze B2 or
MinIO by changing configuration. Nothing outside this module imports boto3.

Credentials are held only in the client this module builds. They are never
logged, never placed in an exception message, and never returned.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

from app.backend.storage.errors import ObjectNotFound, StorageError
from app.backend.storage.keys import validate_key
from app.backend.storage.store import ObjectInfo, StoredObject

try:  # boto3 is needed only for this backend
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]


def _is_missing(exc: "ClientError") -> bool:
    code = str(exc.response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}


class S3DocumentStore:
    backend = "s3"

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        region: str | None,
        access_key_id: str,
        secret_access_key: str,
        key_prefix: str = "",
    ) -> None:
        if boto3 is None:
            raise StorageError("the S3 store needs boto3; install it")
        self._bucket = bucket
        self._prefix = key_prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                # Supabase and most S3-compatible services want path-style.
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def _full(self, key: str) -> str:
        validate_key(key)
        return f"{self._prefix}/{key}" if self._prefix else key

    def _strip(self, full: str) -> str:
        return full[len(self._prefix) + 1 :] if self._prefix else full

    @staticmethod
    def _fail(action: str, exc: Exception) -> StorageError:
        # The boto message can include the endpoint and request ids but not the
        # keys; still, only the exception type and error code are passed on.
        code = ""
        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", ""))
        return StorageError(f"object store {action} failed ({type(exc).__name__} {code})".strip())

    def put(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> StoredObject:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=self._full(key), Body=data, ContentType=content_type
            )
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("write", exc) from None
        return StoredObject(key=key, size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._full(key))
            return response["Body"].read()
        except ClientError as exc:
            if _is_missing(exc):
                raise ObjectNotFound(f"no object at {key}") from None
            raise self._fail("read", exc) from None
        except BotoCoreError as exc:
            raise self._fail("read", exc) from None

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._full(key))
            return True
        except ClientError as exc:
            if _is_missing(exc):
                return False
            raise self._fail("lookup", exc) from None
        except BotoCoreError as exc:
            raise self._fail("lookup", exc) from None

    def delete(self, key: str) -> None:
        try:
            # S3 deletes are idempotent: a missing key is a success.
            self._client.delete_object(Bucket=self._bucket, Key=self._full(key))
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("delete", exc) from None

    def list(self, prefix: str = "") -> Iterator[ObjectInfo]:
        # A prefix is not a key: it may be empty or end in a slash. It is still
        # held to the same character rules, so it cannot name anything odd.
        if prefix:
            validate_key(prefix.rstrip("/") or "x")
        base = f"{self._prefix}/" if self._prefix else ""
        full_prefix = f"{base}{prefix}"
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=full_prefix):
                for item in page.get("Contents", []):
                    yield ObjectInfo(
                        key=self._strip(item["Key"]),
                        size=int(item["Size"]),
                        last_modified=item.get("LastModified"),
                    )
        except (ClientError, BotoCoreError) as exc:
            raise self._fail("listing", exc) from None

    def ensure_bucket(self) -> None:
        """Create the bucket if absent. For tests and first-time local setup only;
        production buckets are created out of band with their access policy."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except (ClientError, BotoCoreError) as exc:
                raise self._fail("bucket creation", exc) from None
