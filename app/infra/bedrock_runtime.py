"""Async-friendly AWS Bedrock runtime client wrapper."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import boto3
from botocore.config import Config


class BedrockInvocationError(RuntimeError):
    """Raised when Bedrock invocation fails or response is malformed."""


class BedrockRuntimeClient:
    """Thin async wrapper around blocking boto3 Bedrock runtime client."""

    def __init__(
        self,
        *,
        region: str,
        max_retries: int,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        aws_session_token: str | None = None,
        endpoint_url: str | None = None,
    ) -> None:
        """Initialize Bedrock runtime client with optional explicit credentials."""

        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
            config=Config(
                retries={"max_attempts": max(1, max_retries), "mode": "standard"},
            ),
        )

    async def invoke_json(
        self,
        *,
        model_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke Bedrock model and return parsed JSON response body."""

        try:
            response = await asyncio.to_thread(
                self._client.invoke_model,
                modelId=model_id,
                body=json.dumps(payload),
                contentType="application/json",
                accept="application/json",
            )
        except Exception as exc:  # pragma: no cover - upstream SDK error surface
            raise BedrockInvocationError(str(exc)) from exc

        body_stream = response.get("body")
        if body_stream is None:
            raise BedrockInvocationError("bedrock response missing body")

        raw_bytes = await asyncio.to_thread(body_stream.read)
        try:
            parsed = json.loads(raw_bytes)
        except Exception as exc:  # pragma: no cover - malformed remote payload
            raise BedrockInvocationError("bedrock response is not valid JSON") from exc

        if not isinstance(parsed, dict):
            raise BedrockInvocationError("bedrock response payload must be a JSON object")
        return parsed
