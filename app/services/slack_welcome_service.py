"""Async service for AI-generated personalized Slack onboarding messages."""

from __future__ import annotations

import json
from typing import Any

import anyio

from app.core.error import ApplicationValidationError
from app.core.runtime_config import (
    BedrockRuntimeConfig,
    EvaluationRuntimeConfig,
)
from app.infra.bedrock_runtime import BedrockInvocationError, BedrockRuntimeClient
from app.schemas.application import ApplicationRecord


class SlackWelcomeService:
    """Generate welcome DM text from candidate profile using the secondary model."""

    def __init__(
        self,
        *,
        bedrock_client: BedrockRuntimeClient,
        bedrock_config: BedrockRuntimeConfig,
        evaluation_config: EvaluationRuntimeConfig,
    ) -> None:
        """Initialize generator with Bedrock + evaluation prompt config."""

        self._bedrock_client = bedrock_client
        self._bedrock_config = bedrock_config
        self._evaluation_config = evaluation_config

    async def generate_welcome_message(
        self,
        *,
        candidate: ApplicationRecord,
        manager_name: str,
        onboarding_links: list[str],
    ) -> str:
        """Generate one Slack welcome message asynchronously."""

        if not self._evaluation_config.slack_welcome_generation_enabled:
            raise ApplicationValidationError("Slack welcome generation is disabled")
        if not self._bedrock_config.enabled:
            raise ApplicationValidationError("LLM Slack welcome generation is disabled")

        selection = candidate.manager_selection_details
        role_title = (
            selection.confirmed_job_title
            if selection is not None
            else candidate.role_selection
        )
        start_date = selection.start_date.isoformat() if selection is not None else "Not specified"
        greeting_from_manager = (
            f"{manager_name} is excited to welcome you to the team."
            if manager_name.strip()
            else "Your hiring manager is excited to welcome you to the team."
        )

        new_hire_profile = {
            "candidate_name": candidate.full_name,
            "candidate_email": str(candidate.email),
            "role": role_title,
            "start_date": start_date,
            "manager_greeting": greeting_from_manager,
        }
        new_hire_profile_json = json.dumps(new_hire_profile, ensure_ascii=True)
        onboarding_links_json = json.dumps(onboarding_links, ensure_ascii=True)

        prompt_template = self._evaluation_config.slack_welcome_prompt_template
        if not prompt_template.strip():
            raise ApplicationValidationError("slack_welcome_prompt_template is empty")
        prompt = prompt_template.format(
            new_hire_profile_json=new_hire_profile_json,
            onboarding_links_json=onboarding_links_json,
        )

        payload: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._bedrock_config.max_tokens,
            "temperature": 0.0,
            "top_p": self._bedrock_config.top_p,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
        }
        try:
            with anyio.fail_after(self._bedrock_config.request_timeout_seconds):
                response = await self._bedrock_client.invoke_json(
                    model_id=self._bedrock_config.fallback_model_id,
                    payload=payload,
                )
        except (BedrockInvocationError, TimeoutError) as exc:
            raise ApplicationValidationError(
                f"failed to generate Slack welcome message: {exc}"
            ) from exc
        except Exception as exc:  # pragma: no cover - unexpected provider/runtime error
            raise ApplicationValidationError(
                f"failed to generate Slack welcome message: {exc}"
            ) from exc

        generated = self._extract_response_text(response).strip()
        if not generated:
            raise ApplicationValidationError("LLM returned empty Slack welcome message")
        return self._clip(generated, max_chars=self._evaluation_config.slack_welcome_max_chars)

    @staticmethod
    def _extract_response_text(response: dict[str, Any]) -> str:
        """Extract text content from Bedrock Anthropic response."""

        content = response.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text_value = item.get("text")
                if isinstance(text_value, str):
                    parts.append(text_value)
            joined = "\n".join(parts).strip()
            if joined:
                return joined

        output_text = response.get("outputText")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        raise ApplicationValidationError("LLM response did not contain Slack welcome text")

    @staticmethod
    def _clip(value: str, *, max_chars: int) -> str:
        """Clip strings to max configured character length."""

        if max_chars <= 0:
            return value
        if len(value) <= max_chars:
            return value
        return value[:max_chars]
