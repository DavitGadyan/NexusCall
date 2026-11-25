"""S3-compatible object storage (ADR-0013). Requires the `nexus-core[aws]` extra.

* Buckets are private; browsers only ever receive short-lived presigned GET URLs.
* Object keys are always prefixed with the tenant id (`tenant_key`) and validated on read
  (`assert_tenant_key`) so a key from one tenant can never be served to another.
* Large payloads live here; queues and events carry only (bucket, key) references.
"""

from __future__ import annotations

import re
import uuid
from contextlib import AbstractAsyncContextManager
from typing import Any

from nexus_core.config import ServiceSettings
from nexus_core.errors import Forbidden

BUCKETS = frozenset({"recordings", "transcripts", "attachments", "raw-mail", "imports", "exports", "knowledge"})
_SAFE_PART = re.compile(r"^[A-Za-z0-9._=-]{1,200}$")
DEFAULT_PRESIGN_SECONDS = 300
MAX_PRESIGN_SECONDS = 900


def tenant_key(tenant_id: uuid.UUID | str, *parts: str) -> str:
    """Build `<tenant>/<part>/<part>`; every part must be a safe path segment (no slashes, no `..`)."""
    tid = str(uuid.UUID(str(tenant_id)))
    for part in parts:
        if not _SAFE_PART.match(part) or part in (".", ".."):
            raise ValueError(f"Unsafe object key segment: {part!r}")
    return "/".join([tid, *parts])


def assert_tenant_key(tenant_id: uuid.UUID | str, key: str) -> str:
    if not key.startswith(f"{uuid.UUID(str(tenant_id))}/") or ".." in key.split("/"):
        raise Forbidden("Object does not belong to this tenant")
    return key


class ObjectStore:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        public_endpoint_url: str | None = None,
    ) -> None:
        self._endpoint = endpoint_url
        self._public_endpoint = public_endpoint_url or endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region

    @classmethod
    def from_settings(cls, settings: ServiceSettings) -> ObjectStore:
        if settings.s3_access_key_id is None or settings.s3_secret_access_key is None:
            raise RuntimeError("Object storage credentials are not configured (S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY)")
        return cls(
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key_id.get_secret_value(),
            secret_key=settings.s3_secret_access_key.get_secret_value(),
            region=settings.s3_region,
            public_endpoint_url=settings.s3_public_endpoint_url,
        )

    def _client(self, *, public: bool = False) -> AbstractAsyncContextManager[Any]:
        from aiobotocore.config import AioConfig
        from aiobotocore.session import get_session

        return get_session().create_client(  # type: ignore[no-any-return]
            "s3",
            endpoint_url=self._public_endpoint if public else self._endpoint,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=AioConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @staticmethod
    def _check_bucket(bucket: str) -> str:
        if bucket not in BUCKETS:
            raise ValueError(f"Unknown bucket: {bucket}")
        return bucket

    async def put(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        async with self._client() as s3:
            await s3.put_object(
                Bucket=self._check_bucket(bucket), Key=key, Body=data, ContentType=content_type, Metadata=metadata or {}
            )

    async def get(self, bucket: str, key: str, *, max_bytes: int | None = None) -> bytes:
        async with self._client() as s3:
            resp = await s3.get_object(Bucket=self._check_bucket(bucket), Key=key)
            if max_bytes is not None and int(resp.get("ContentLength", 0)) > max_bytes:
                raise ValueError("Object exceeds the allowed size")
            async with resp["Body"] as stream:
                return bytes(await stream.read())

    async def exists(self, bucket: str, key: str) -> bool:
        from botocore.exceptions import ClientError

        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=self._check_bucket(bucket), Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise
        return True

    async def delete(self, bucket: str, key: str) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._check_bucket(bucket), Key=key)

    async def presign_get(
        self,
        bucket: str,
        key: str,
        *,
        expires_seconds: int = DEFAULT_PRESIGN_SECONDS,
        download_filename: str | None = None,
    ) -> str:
        """Short-lived, browser-reachable download URL. Callers must authorize and audit before calling."""
        params: dict[str, Any] = {"Bucket": self._check_bucket(bucket), "Key": key}
        if download_filename:
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", download_filename)[:120] or "download"
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        async with self._client(public=True) as s3:
            return str(
                await s3.generate_presigned_url(
                    "get_object", Params=params, ExpiresIn=max(1, min(expires_seconds, MAX_PRESIGN_SECONDS))
                )
            )

    async def ping(self) -> bool:
        async with self._client() as s3:
            await s3.list_buckets()
        return True


class InMemoryObjectStore(ObjectStore):
    """Deterministic store for unit tests (never used outside tests)."""

    def __init__(self) -> None:
        super().__init__(endpoint_url="memory://", access_key="test", secret_key="test")  # noqa: S106
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    async def put(
        self,
        bucket: str,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.objects[(self._check_bucket(bucket), key)] = (data, content_type)

    async def get(self, bucket: str, key: str, *, max_bytes: int | None = None) -> bytes:
        data = self.objects[(self._check_bucket(bucket), key)][0]
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError("Object exceeds the allowed size")
        return data

    async def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    async def delete(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    async def presign_get(
        self,
        bucket: str,
        key: str,
        *,
        expires_seconds: int = DEFAULT_PRESIGN_SECONDS,
        download_filename: str | None = None,
    ) -> str:
        return f"memory://{self._check_bucket(bucket)}/{key}?expires={min(expires_seconds, MAX_PRESIGN_SECONDS)}"

    async def ping(self) -> bool:
        return True
