"""Tests for research quality telemetry in the SQS enrichment worker."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from app.core.runtime_config import BedrockRuntimeConfig, ResearchRuntimeConfig
from app.scripts.sqs_research_enrichment_worker import SqsResearchEnrichmentWorker


class _DummyQueueClient:
    """Minimal queue client placeholder for worker construction."""


def test_record_telemetry_counts_manual_low_confidence_and_fallback_usage() -> None:
    """Telemetry counters should increment for manual review/low confidence/fallback signals."""

    research_config = ResearchRuntimeConfig()
    bedrock_config = BedrockRuntimeConfig(fallback_model_id="fallback-model")
    worker = SqsResearchEnrichmentWorker(
        queue_client=_DummyQueueClient(),
        research_config=research_config,
        bedrock_config=bedrock_config,
        bedrock_client=None,
        max_in_flight=1,
        receive_batch_size=1,
        receive_wait_seconds=1,
        visibility_timeout_seconds=30,
    )

    payload = {
        "deterministic_checks": {
            "manual_review_required": True,
            "confidence_baseline": "low",
        },
        "issue_flags": [{"severity": "high"}, {"severity": "low"}],
        "llm_analysis": {
            "source": "heuristic_fallback",
            "model_id": "fallback-model",
            "confidence": "low",
        },
    }

    asyncio.run(worker._record_telemetry(application_id=uuid4(), payload=payload))

    assert worker._telemetry.processed == 1
    assert worker._telemetry.manual_review_required == 1
    assert worker._telemetry.low_confidence == 1
    assert worker._telemetry.high_severity_flags == 1
    assert worker._telemetry.parse_failures == 1
    assert worker._telemetry.fallback_model_usage == 1
    assert worker._telemetry.heuristic_fallback_usage == 1


def test_record_telemetry_does_not_count_parse_failure_for_model_source() -> None:
    """Model-source payload should not be recorded as parse failure."""

    research_config = ResearchRuntimeConfig()
    bedrock_config = BedrockRuntimeConfig(primary_model_id="primary-model")
    worker = SqsResearchEnrichmentWorker(
        queue_client=_DummyQueueClient(),
        research_config=research_config,
        bedrock_config=bedrock_config,
        bedrock_client=None,
        max_in_flight=1,
        receive_batch_size=1,
        receive_wait_seconds=1,
        visibility_timeout_seconds=30,
    )

    payload = {
        "deterministic_checks": {
            "manual_review_required": False,
            "confidence_baseline": "medium",
        },
        "issue_flags": [],
        "llm_analysis": {
            "source": "model",
            "model_id": "primary-model",
            "confidence": "medium",
        },
    }

    asyncio.run(worker._record_telemetry(application_id=uuid4(), payload=payload))

    assert worker._telemetry.processed == 1
    assert worker._telemetry.manual_review_required == 0
    assert worker._telemetry.low_confidence == 0
    assert worker._telemetry.high_severity_flags == 0
    assert worker._telemetry.parse_failures == 0
    assert worker._telemetry.fallback_model_usage == 0
    assert worker._telemetry.heuristic_fallback_usage == 0
