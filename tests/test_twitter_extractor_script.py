"""Tests for Twitter extractor helper functions."""

from app.scripts.extract_twitter_profile import _extract_topics
from app.scripts.extract_twitter_profile import _parse_username


def test_parse_username_supports_handle_url_and_plain_value() -> None:
    """Username parser should support common handle formats."""

    assert _parse_username("@BinitSapkota1") == "BinitSapkota1"
    assert _parse_username("https://x.com/BinitSapkota1") == "BinitSapkota1"
    assert _parse_username("BinitSapkota1") == "BinitSapkota1"


def test_extract_topics_prefers_hashtags_and_keywords() -> None:
    """Topic extractor should emit hashtag and token signals."""

    posts = [
        {"text": "Building #LLM pipelines with python and fastapi"},
        {"text": "Shipping #LLM and #RAG systems for production"},
        {"text": "python fastapi deployment and monitoring"},
    ]
    topics = _extract_topics(posts, max_items=5)
    assert "#llm" in topics
    assert any(item in topics for item in ("python", "fastapi", "#rag"))
