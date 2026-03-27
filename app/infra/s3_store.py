"""Async-friendly S3 object store wrapper."""

from __future__ import annotations

import json
import os
from typing import Any

import anyio
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.runtime_config import S3StorageRuntimeConfig


class S3ObjectNotFoundError(ValueError):
    """Raised when an S3 object does not exist."""


class S3ObjectAlreadyExistsError(ValueError):
    """Raised when a conditional S3 put finds an existing object."""


class S3ObjectStore:
    """Thread-wrapped S3 client for non-blocking async use."""

    def __init__(self, config: S3StorageRuntimeConfig):
        """Initialize store with env settings and runtime storage config."""

        self._bucket = config.bucket
        self._list_page_size = config.list_page_size

        s3_config_kwargs: dict[str, dict[str, str]] = {}
        if config.force_path_style:
            s3_config_kwargs["s3"] = {"addressing_style": "path"}

        client_config = Config(**s3_config_kwargs) if s3_config_kwargs else None
        self._client = boto3.client(
            "s3",
            region_name=config.region,
            config=client_config,
        )
        self._transfer_config = TransferConfig(
            max_concurrency=config.upload_max_concurrency,
            multipart_threshold=config.upload_multipart_threshold_mb * 1024 * 1024,
            multipart_chunksize=config.upload_multipart_chunksize_mb * 1024 * 1024,
            use_threads=True,
        )

    async def put_json(
        self,
        key: str,
        payload: dict[str, Any],
        *,
        if_none_match: str | None = None,
    ) -> None:
        """Store JSON payload at key, optionally with conditional write."""

        body = json.dumps(payload).encode("utf-8")

        def _run() -> None:
            kwargs: dict[str, Any] = {
                "Bucket": self._bucket,
                "Key": key,
                "Body": body,
                "ContentType": "application/json",
            }
            if if_none_match is not None:
                kwargs["IfNoneMatch"] = if_none_match
            self._client.put_object(**kwargs)

        try:
            await anyio.to_thread.run_sync(_run)
        except ClientError as exc:
            if self._is_precondition_failed(exc):
                raise S3ObjectAlreadyExistsError(key) from exc
            raise

    async def put_bytes(
        self,
        *,
        key: str,
        payload: bytes,
        content_type: str,
    ) -> None:
        """Store raw bytes payload at key with explicit content type."""

        def _run() -> None:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
            )

        await anyio.to_thread.run_sync(_run)

    async def get_json(self, key: str) -> dict[str, Any]:
        """Fetch and decode JSON object from S3 key."""

        def _run() -> dict[str, Any]:
            result = self._client.get_object(Bucket=self._bucket, Key=key)
            raw_body = result["Body"].read()
            return json.loads(raw_body.decode("utf-8"))

        try:
            return await anyio.to_thread.run_sync(_run)
        except ClientError as exc:
            if self._is_not_found(exc):
                raise S3ObjectNotFoundError(key) from exc
            raise

    async def get_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        """Fetch raw object bytes from S3 key."""

        target_bucket = bucket or self._bucket

        def _run() -> bytes:
            result = self._client.get_object(Bucket=target_bucket, Key=key)
            return bytes(result["Body"].read())

        try:
            return await anyio.to_thread.run_sync(_run)
        except ClientError as exc:
            if self._is_not_found(exc):
                raise S3ObjectNotFoundError(key) from exc
            raise

    async def exists(self, key: str) -> bool:
        """Return True when object exists in S3."""

        def _run() -> None:
            self._client.head_object(Bucket=self._bucket, Key=key)

        try:
            await anyio.to_thread.run_sync(_run)
            return True
        except ClientError as exc:
            if self._is_not_found(exc):
                return False
            raise

    async def delete(self, key: str) -> None:
        """Delete object by key (idempotent)."""

        await anyio.to_thread.run_sync(
            lambda: self._client.delete_object(Bucket=self._bucket, Key=key)
        )

    async def list_keys(self, prefix: str) -> list[str]:
        """List all object keys under a prefix."""

        def _run() -> list[str]:
            paginator = self._client.get_paginator("list_objects_v2")
            keys: list[str] = []
            for page in paginator.paginate(
                Bucket=self._bucket,
                Prefix=prefix,
                PaginationConfig={"PageSize": self._list_page_size},
            ):
                for item in page.get("Contents", []):
                    keys.append(item["Key"])
            return keys

        return await anyio.to_thread.run_sync(_run)

    async def generate_presigned_get_url(
        self,
        *,
        key: str,
        expires_in_seconds: int,
        bucket: str | None = None,
        response_content_disposition: str | None = None,
    ) -> str:
        """Generate a temporary pre-signed GET URL for one object."""

        target_bucket = bucket or self._bucket

        def _run() -> str:
            params: dict[str, Any] = {
                "Bucket": target_bucket,
                "Key": key,
            }
            if response_content_disposition:
                params["ResponseContentDisposition"] = response_content_disposition
            return str(
                self._client.generate_presigned_url(
                    "get_object",
                    Params=params,
                    ExpiresIn=max(60, expires_in_seconds),
                )
            )

        return await anyio.to_thread.run_sync(_run)

    async def upload_fileobj(
        self,
        *,
        key: str,
        file_obj: Any,
        content_type: str,
        max_bytes: int,
    ) -> int:
        """Upload file object to S3 and enforce max file size."""

        def _run() -> int:
            file_obj.seek(0, os.SEEK_END)
            size = int(file_obj.tell())
            file_obj.seek(0)

            if size <= 0:
                raise ValueError("resume file is empty")
            if size > max_bytes:
                raise ValueError("resume file exceeds maximum size")

            self._client.upload_fileobj(
                Fileobj=file_obj,
                Bucket=self._bucket,
                Key=key,
                ExtraArgs={"ContentType": content_type},
                Config=self._transfer_config,
            )
            return size

        return await anyio.to_thread.run_sync(_run)

    @staticmethod
    def _is_not_found(exc: ClientError) -> bool:
        """Return True when error code represents missing object."""

        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        return code in {"NoSuchKey", "404", "NotFound"} or status == 404

    @staticmethod
    def _is_precondition_failed(exc: ClientError) -> bool:
        """Return True when conditional write precondition fails."""

        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        return code in {"PreconditionFailed", "412"} or status == 412
