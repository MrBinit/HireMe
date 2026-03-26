"""Tests for shortlisted candidate enrichment helper functions."""

import json
import re

from app.core.runtime_config import ResearchRuntimeConfig
from app.scripts.enrich_shortlisted_candidates import (
    _build_candidate_brief,
    _build_github_enrichment_payload,
    _build_discrepancies,
    _extract_resume_skills_and_employers,
    _normalize_brief_text,
    _parse_github_username,
    _serialize_payload_with_limit,
)


def test_parse_github_username_extracts_name() -> None:
    """GitHub username parser should extract owner segment."""

    assert _parse_github_username("https://github.com/mrbinit") == "mrbinit"
    assert _parse_github_username("https://github.com/mrbinit/HireMe") == "mrbinit"
    assert _parse_github_username("https://example.com/mrbinit") is None


def test_extract_resume_skills_and_employers_reads_parse_result() -> None:
    """Resume helper should return distinct skills and employer names."""

    parse_result = {
        "skills": ["Python", "FastAPI", "Python"],
        "work_experience": [
            {"company": "Qualz.AI"},
            {"company": "Wiseyak"},
            {"company": "Qualz.AI"},
        ],
    }
    skills, employers = _extract_resume_skills_and_employers(parse_result)
    assert skills == ["Python", "FastAPI"]
    assert employers == ["Qualz.AI", "Wiseyak"]


def test_build_discrepancies_prioritizes_core_gaps() -> None:
    """Discrepancy builder should include employer/skill/github/portfolio checks."""

    discrepancies = _build_discrepancies(
        missing_employers=["Wiseyak"],
        missing_skills=["FastAPI", "PostgreSQL"],
        github_repo_count=0,
        portfolio_hit_count=0,
        max_items=8,
    )
    assert any("employers" in item for item in discrepancies)
    assert any("skills" in item for item in discrepancies)
    assert any("repositories" in item for item in discrepancies)
    assert any("portfolio" in item for item in discrepancies)


def test_build_candidate_brief_returns_3_to_5_sentences() -> None:
    """Brief should stay within configured sentence range."""

    brief = _build_candidate_brief(
        full_name="Binit Sapkota",
        role_selection="Backend Engineer",
        years_experience=2.5,
        matched_employers=["Qualz.AI", "Wiseyak"],
        github_repo_count=12,
        github_top_repos=["hireme", "llm-eval"],
        twitter_hit_count=3,
        portfolio_hit_count=2,
        discrepancies=["Resume employers not clearly found."],
        min_sentences=3,
        max_sentences=5,
    )
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", brief))
    assert 3 <= sentence_count <= 5


def test_build_github_enrichment_payload_has_profile_repo_and_derived_sections() -> None:
    """GitHub payload should include requested profile/repo/derived blocks."""

    config = ResearchRuntimeConfig()
    user_payload = {
        "login": "mrbinit",
        "bio": "Backend engineer",
        "public_repos": 12,
        "followers": 34,
    }
    repos_payload = [
        {
            "name": "hireme",
            "description": "Hiring system",
            "stargazers_count": 8,
            "forks_count": 3,
            "language": "Python",
            "updated_at": "2026-03-20T10:00:00Z",
            "topics": ["hiring", "fastapi"],
            "html_url": "https://github.com/mrbinit/hireme",
        },
        {
            "name": "portfolio",
            "description": "Personal portfolio",
            "stargazers_count": 2,
            "forks_count": 1,
            "language": "TypeScript",
            "updated_at": "2025-01-10T10:00:00Z",
            "topics": ["nextjs"],
            "html_url": "https://github.com/mrbinit/portfolio",
        },
    ]

    payload = _build_github_enrichment_payload(
        github_url="https://github.com/mrbinit",
        username="mrbinit",
        user_payload=user_payload,
        repos_payload=repos_payload,
        config=config,
    )

    profile_info = payload["profile_info"]
    assert profile_info["username"] == "mrbinit"
    assert profile_info["bio"] == "Backend engineer"
    assert profile_info["public_repos"] == 12
    assert profile_info["followers"] == 34
    assert payload["repo_info"][0]["name"] == "hireme"
    assert payload["repo_info"][0]["stars"] == 8
    assert "topics" in payload["repo_info"][0]
    assert payload["final_derived"]["top_3_projects"][0]["name"] == "hireme"
    assert "Python" in payload["final_derived"]["primary_languages"]
    assert payload["final_derived"]["activity_status"] in {"active", "inactive"}


def test_normalize_brief_text_clamps_to_max_sentences() -> None:
    """Brief normalization should clamp long model output to configured sentence cap."""

    normalized = _normalize_brief_text(
        text=(
            "Sentence one. Sentence two. Sentence three. "
            "Sentence four. Sentence five. Sentence six."
        ),
        min_sentences=3,
        max_sentences=5,
        fallback_text="Fallback one. Fallback two. Fallback three.",
    )
    sentence_count = len(re.findall(r"[.!?](?:\s|$)", normalized))
    assert sentence_count == 5


def test_serialize_payload_with_limit_keeps_valid_json() -> None:
    """Storage serializer should return valid JSON within max size."""

    payload = {
        "candidate_id": "abc",
        "brief": "x" * 1200,
        "linkedin": {
            "profile_url": "https://linkedin.com/in/test",
            "hits": [
                {"title": "a" * 300, "snippet": "b" * 300, "link": "https://linkedin.com/in/test"}
                for _ in range(8)
            ],
        },
        "twitter": {"profile_url": "https://x.com/test", "hits": []},
        "portfolio": {"url": "https://test.dev", "hits": []},
        "github": {
            "profile_info": {
                "username": "test",
                "bio": "bio",
                "public_repos": 20,
                "followers": 10,
            },
            "repo_info": [
                {
                    "name": f"repo-{idx}",
                    "description": "d" * 350,
                    "stars": idx,
                    "forks": idx,
                    "language": "Python",
                    "updated_at": "2026-03-20T10:00:00Z",
                    "topics": ["a", "b", "c", "d", "e", "f"],
                    "html_url": f"https://github.com/test/repo-{idx}",
                }
                for idx in range(20)
            ],
            "final_derived": {
                "top_3_projects": [],
                "primary_languages": ["Python"],
                "activity_status": "active",
            },
        },
    }

    serialized = _serialize_payload_with_limit(payload, max_chars=1200)
    assert len(serialized) <= 1200
    parsed = json.loads(serialized)
    assert isinstance(parsed, dict)
