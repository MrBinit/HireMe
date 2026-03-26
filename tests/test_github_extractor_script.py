"""Tests for GitHub extractor helper functions."""

from app.scripts.extract_github_profile import (
    _derive_activity_status,
    _derive_primary_languages,
    _parse_github_username,
    _rank_top_repos,
    _summarize_readme,
)


def test_parse_github_username_from_url_or_plain_value() -> None:
    """Username parser should accept profile URL and plain username."""

    assert _parse_github_username("https://github.com/mrbinit") == "mrbinit"
    assert _parse_github_username("https://github.com/mrbinit/HireMe") == "mrbinit"
    assert _parse_github_username("mrbinit") == "mrbinit"
    assert _parse_github_username("https://example.com/mrbinit") is None


def test_rank_top_repos_orders_by_stars_and_skips_forks() -> None:
    """Repo ranking should prioritize stars and skip forked repositories."""

    repos = [
        {"name": "forked", "fork": True, "stargazers_count": 100, "forks_count": 0},
        {"name": "alpha", "fork": False, "stargazers_count": 10, "forks_count": 2},
        {"name": "beta", "fork": False, "stargazers_count": 5, "forks_count": 9},
    ]
    ranked = _rank_top_repos(repos, max_items=2)
    assert [item["name"] for item in ranked] == ["alpha", "beta"]


def test_derive_primary_languages_returns_frequency_order() -> None:
    """Language derivation should order by repository frequency."""

    repos = [
        {"language": "Python"},
        {"language": "Python"},
        {"language": "TypeScript"},
        {"language": "Go"},
    ]
    derived = _derive_primary_languages(repos, max_items=3)
    assert derived == ["Python", "Go", "TypeScript"] or derived == [
        "Python",
        "TypeScript",
        "Go",
    ]


def test_summarize_readme_strips_markdown_noise() -> None:
    """README summary should return compact plain-text output."""

    summary = _summarize_readme(
        """# Project Title

This is a **production** backend API for hiring workflow.

## Features
- Parse resumes
- Score candidates
""",
        max_chars=120,
    )
    assert isinstance(summary, str)
    assert "Project Title" in summary
    assert "backend API" in summary


def test_derive_activity_status_marks_recent_repo_as_active() -> None:
    """Activity status should be active when push date is recent."""

    repos = [{"pushed_at": "2026-03-20T10:00:00Z"}]
    assert _derive_activity_status(repos, active_within_days=180) == "active"
