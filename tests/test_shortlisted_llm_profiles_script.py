"""Tests for shortlisted LLM profile enrichment helper logic."""

from uuid import uuid4

from app.scripts.enrich_shortlisted_llm_profiles import CandidateSeed
from app.scripts.enrich_shortlisted_llm_profiles import _build_compact_storage_payload
from app.scripts.enrich_shortlisted_llm_profiles import _build_issue_flags
from app.scripts.enrich_shortlisted_llm_profiles import _build_mock_twitter_payload
from app.scripts.enrich_shortlisted_llm_profiles import _cross_check_resume_vs_github
from app.scripts.enrich_shortlisted_llm_profiles import _cross_check_resume_vs_linkedin


def test_cross_check_resume_vs_linkedin_reads_existing_cross_reference() -> None:
    """LinkedIn cross-check should reuse extractor cross-reference fields."""

    resume_signals = {
        "skills": ["Python", "FastAPI"],
        "employers": ["Acme"],
        "positions": ["Backend Engineer"],
        "projects": ["Hiring API"],
    }
    linkedin_payload = {
        "cross_reference": {
            "skills": {
                "matched_on_linkedin": ["Python"],
                "unmatched_from_resume": ["FastAPI"],
            },
            "employment_history": {
                "matched_employers_on_linkedin": [],
                "unmatched_employers_from_resume": ["Acme"],
                "matched_positions_on_linkedin": [],
                "unmatched_positions_from_resume": ["Backend Engineer"],
            },
        }
    }

    result = _cross_check_resume_vs_linkedin(
        resume_signals=resume_signals,
        linkedin_payload=linkedin_payload,
    )
    assert result["matched_skills"] == ["Python"]
    assert result["skill_differences"] is True
    assert result["experience_mismatch"] is True


def test_cross_check_resume_vs_github_matches_skills_and_projects() -> None:
    """GitHub cross-check should align resume signals to repo evidence corpus."""

    resume_signals = {
        "skills": ["Python", "FastAPI", "PostgreSQL"],
        "employers": [],
        "positions": [],
        "projects": ["hireme", "Resume Parser Pipeline"],
    }
    github_payload = {
        "top_repositories": [
            {
                "name": "hireme",
                "language": "Python",
                "description": "FastAPI hiring system",
                "readme_summary": "Resume parser pipeline and candidate scoring",
                "topics": ["fastapi", "postgresql"],
            }
        ],
        "aggregate": {"top_languages": ["Python"]},
    }

    result = _cross_check_resume_vs_github(
        resume_signals=resume_signals,
        github_payload=github_payload,
    )
    assert "Python" in result["matched_skills"]
    assert "FastAPI" in result["matched_skills"]
    assert "PostgreSQL" in result["matched_skills"]
    assert "hireme" in result["matched_projects"]
    assert result["missing_projects_flag"] is False


def test_build_issue_flags_and_mock_twitter_payload() -> None:
    """Issue flags should include required types; Twitter payload should stay mocked."""

    linkedin_check = {
        "unmatched_employers": ["Acme"],
        "unmatched_positions": ["Backend Engineer"],
        "unmatched_skills": ["FastAPI"],
        "experience_mismatch": True,
        "skill_differences": True,
    }
    github_check = {
        "missing_projects": ["Hiring API"],
        "unmatched_skills": ["Kubernetes"],
        "skill_differences": True,
    }
    flags = _build_issue_flags(
        linkedin_check=linkedin_check,
        github_check=github_check,
    )
    flag_types = [item["type"] for item in flags]
    assert "experience_mismatch" in flag_types
    assert "missing_projects" in flag_types
    assert "skill_differences" in flag_types

    candidate = CandidateSeed(
        id=uuid4(),
        full_name="Test Candidate",
        role_selection="Backend Engineer",
        applicant_status="shortlisted",
        linkedin_url="https://linkedin.com/in/test",
        twitter_url="https://x.com/test",
        github_url="https://github.com/test",
        portfolio_url="https://flowcv.me/test",
        parse_result={},
    )
    twitter_payload = _build_mock_twitter_payload(candidate)
    assert twitter_payload["mode"] == "mock"
    assert twitter_payload["profile_url"] == "https://x.com/test"


def test_build_compact_storage_payload_keeps_portfolio_signals() -> None:
    """Compacted payload should preserve key portfolio extraction fields."""

    compact = _build_compact_storage_payload(
        {
            "generated_at": "2026-03-26T00:00:00Z",
            "candidate_id": "abc",
            "candidate_name": "Test Candidate",
            "role_selection": "Backend Engineer",
            "links_from_candidate_table": {"portfolio_url": "https://flowcv.me/test"},
            "resume_snapshot": {
                "skills": ["Python"],
                "projects": ["HireMe"],
                "work_experience": [],
            },
            "extractors": {
                "linkedin": {},
                "github": {},
                "twitter": {},
                "portfolio": {
                    "mode": "serpapi",
                    "input_portfolio_url": "https://flowcv.me/test",
                    "matched_portfolio_url": "https://flowcv.me/test",
                    "technology_signals": ["Python", "FastAPI"],
                    "project_signals": ["HireMe"],
                    "top_portfolio_hits": [
                        {
                            "title": "FlowCV profile",
                            "link": "https://flowcv.me/test",
                            "snippet": "Projects and backend work",
                        }
                    ],
                },
            },
            "cross_checks": {"resume_vs_linkedin": {}, "resume_vs_github": {}},
            "issue_flags": [],
            "llm_analysis": {"cross_reference": {}},
            "discrepancies": [],
            "brief": "Short brief.",
        }
    )

    portfolio = compact["extractors"]["portfolio"]
    assert portfolio["mode"] == "serpapi"
    assert portfolio["matched_portfolio_url"] == "https://flowcv.me/test"
    assert "Python" in portfolio["technology_signals"]
