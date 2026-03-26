"""Tests for SerpAPI online-research script helper functions."""

from app.scripts.run_online_research import (
    _build_summary,
    _extract_hits,
    _matches_domain,
    _pick_first_link,
)


def test_extract_hits_returns_top_valid_links() -> None:
    """Hit extraction should keep valid links and enforce max length."""

    payload = {
        "organic_results": [
            {"link": "https://example.com/a", "title": "A", "snippet": "S1"},
            {"link": "https://example.com/b", "title": "B", "snippet": "S2"},
            {"link": "not-a-url", "title": "C", "snippet": "S3"},
        ]
    }
    hits = _extract_hits(payload, max_hits=2)
    assert len(hits) == 2
    assert hits[0]["link"] == "https://example.com/a"
    assert hits[1]["title"] == "B"


def test_matches_domain_supports_subdomain() -> None:
    """Domain matcher should allow exact and subdomain matches."""

    assert _matches_domain("https://www.linkedin.com/in/test", ("linkedin.com",))
    assert not _matches_domain("https://example.com/user", ("linkedin.com",))


def test_pick_first_link_prefers_allowed_domain() -> None:
    """Picker should return first hit matching allowed domain list."""

    hits = [
        {"link": "https://example.com/profile"},
        {"link": "https://x.com/test-user"},
    ]
    picked = _pick_first_link(hits, allowed_domains=("x.com", "twitter.com"))
    assert picked == "https://x.com/test-user"


def test_build_summary_truncates_to_max_chars() -> None:
    """Summary builder should clip output to configured max length."""

    summary = _build_summary(
        full_name="Candidate",
        role_selection="Backend Engineer",
        linkedin_url=None,
        twitter_url=None,
        profile_hits=[{"title": "Profile", "snippet": "Long text", "link": "https://e.com"}],
        linkedin_hits=[],
        twitter_hits=[],
        max_chars=40,
    )
    assert len(summary) <= 40
